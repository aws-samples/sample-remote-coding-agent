"""The system of record: three DynamoDB tables and the two queue pairs that feed them.

This stack is separated from the other three by *lifecycle*, not by tidiness. All three
tables are `RemovalPolicy.RETAIN` (invariant 22), because the teardown path for a
repository is `DELETE /configurations/{id}` -- which deletes that configuration's rows
and its runtime and nothing else -- and never `cdk destroy`, which would take the whole
fleet's history with it. A `RETAIN` table outlives every other stack in the app, so it
has to be alone in one.

The key schema on `Tasks` is the decision in here worth reading twice: partition
`configId` **plus** sort `taskId`, and no `byConfig` GSI. A GSI is the obvious way to
answer "what has this repository been doing" and it is the wrong one; the comment on the
table says why (invariant 31, Tier 1 #7-#8).

`Cards` is the mirror image and is left with its GSI: the container never touches that
table, so there is no isolation argument to make, and a board read genuinely is a query
across cards.

No physical names anywhere -- not on the tables, not on the queues. The names reach the
handlers as Lambda environment variables (`CONFIG_TABLE`, `TASK_QUEUE_URL`, ...), so a
fixed name buys nothing and costs the one thing that matters: with a name set,
CloudFormation cannot replace the resource, and a change that needs a replacement
becomes a failed deploy instead of a slow one.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class DataStack(cdk.Stack):
    """The three tables and the two queue pairs, exposed as attributes `ApiStack` reads.

    `configurations`, `tasks`, `cards`, `task_queue`, `task_dlq`, `provision_queue` and
    `provision_dlq` are the public surface: `ApiStack` grants against these objects and
    puts their names and URLs in every handler's environment.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, app_name: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Every physical name in this stack is left to CloudFormation, so `app_name`
        # lands as a tag rather than as a prefix: it is what separates two deployments
        # sharing one account in a cost report, and unlike a name a tag survives the
        # replacement leaving the names unset exists to allow.
        cdk.Tags.of(self).add("Application", app_name)

        self.configurations = dynamodb.TableV2(
            self,
            "Configurations",
            partition_key=dynamodb.Attribute(name="configId", type=dynamodb.AttributeType.STRING),
            # One index, and only because `configurations.create` has to reject a second
            # configuration for a repository that already has one. `configurations.list_`
            # scans instead -- a fleet is tens of rows, and an index maintained for a
            # scan that costs nothing is an index that can disagree with the table.
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="byRepository",
                    partition_key=dynamodb.Attribute(
                        name="repository", type=dynamodb.AttributeType.STRING
                    ),
                    projection_type=dynamodb.ProjectionType.ALL,
                )
            ],
            billing=dynamodb.Billing.on_demand(),
            # On, on all three. The realistic incident is not a region loss, it is a
            # handler writing the wrong thing to a row nobody looks at for a week; these
            # rows are the only record of what an autonomous agent did.
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # Invariant 22: `DELETE /configurations/{id}` is the teardown path, not
            # `cdk destroy`.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # Partition `configId`, sort `taskId`, and deliberately **no** `byConfig` GSI --
        # the absence is the design, not an omission:
        #
        # * The base table is already ordered by configuration, so one repository's
        #   history is a single `query` on the base table. A GSI would answer the same
        #   question a second way, eventually consistently.
        # * It is what makes a `dynamodb:LeadingKeys` condition able to fence a container
        #   to its own repository's tasks (invariant 31, Tier 1 #7-#8). A condition on
        #   the base table's partition key does not apply to a query against an index, so
        #   adding one here would hand every container a way around its own fence.
        # * Creation ordering comes from `taskId` being time-sortable
        #   (`tsk_<epoch-ms>_<hex>`), not from an index's sort key -- which is why the id
        #   format is a contract and not a cosmetic choice.
        self.tasks = dynamodb.TableV2(
            self,
            "Tasks",
            partition_key=dynamodb.Attribute(name="configId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="taskId", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # Invariant 22, as above.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        self.cards = dynamodb.TableV2(
            self,
            "Cards",
            partition_key=dynamodb.Attribute(name="cardId", type=dynamodb.AttributeType.STRING),
            # `byConfig` stays here, unlike on `Tasks`: no container ever reads this
            # table, so there is no isolation fence for an index to route around, and a
            # board read really is "every card for this configuration, newest first".
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="byConfig",
                    partition_key=dynamodb.Attribute(
                        name="configId", type=dynamodb.AttributeType.STRING
                    ),
                    sort_key=dynamodb.Attribute(
                        name="createdAt", type=dynamodb.AttributeType.STRING
                    ),
                    projection_type=dynamodb.ProjectionType.ALL,
                )
            ],
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # Invariant 22, as above.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # Six minutes on both pairs, which clears the provisioner's 5-minute Lambda
        # timeout with a minute of margin. The queues keep the default
        # `RemovalPolicy.DESTROY`: a queue is a buffer, not a system of record, and
        # invariant 22 is a statement about the tables.
        self.task_queue, self.task_dlq = self._queue_with_dlq(
            "Task", visibility=cdk.Duration.minutes(6)
        )
        self.provision_queue, self.provision_dlq = self._queue_with_dlq(
            "Provision", visibility=cdk.Duration.minutes(6)
        )

    def _queue_with_dlq(
        self, name: str, *, visibility: cdk.Duration
    ) -> tuple[sqs.Queue, sqs.Queue]:
        """Create `{name}Queue` and `{name}Dlq`, wired by a redrive policy.

        One helper for both pairs so they cannot drift apart, mirroring
        `tests/conftest.py::_queue_with_dlq`. That fixture and this method are two
        spellings of one shape, and the suite's `maxReceiveCount` of 3 is only evidence
        about a deployed queue while this stays 3 as well.
        """
        dlq = sqs.Queue(
            self,
            f"{name}Dlq",
            # 14 days, the SQS maximum. A poison message that arrived on Friday evening
            # is still readable on Monday morning; the four-day default is not enough for
            # that, and a DLQ nobody got to in time told nobody anything.
            retention_period=cdk.Duration.days(14),
            # A DLQ carries the same message bodies as the queue in front of it, so it
            # gets the same transport requirement.
            enforce_ssl=True,
            # The same visibility as the queue in front of it, and for a stronger reason
            # than symmetry: `TaskDlq` is itself consumed, by `ApiStack`'s `dlq_handler`
            # (`Duration.minutes(1)`), and SQS event source mappings are rejected outright
            # when a queue's visibility timeout is below its consumer's function timeout.
            # Left at the 30-second default this failed at `CreateEventSourceMapping` --
            # "Queue visibility timeout: 30 seconds is less than Function timeout: 60
            # seconds" -- which rolled the whole of `ApiStack` back on a first deploy.
            #
            # Nothing caught it earlier because nothing could: this file's docstring pair
            # in `tests/test_grant_contract.py` records that `ApiStack` bundles `../api`
            # through Docker at construction time, so no test in this repository
            # synthesizes it, and the constraint is enforced by SQS at create time rather
            # than by CDK at synth time. The pairing is therefore a comment and a shared
            # `visibility` argument rather than an assertion.
            visibility_timeout=visibility,
        )
        queue = sqs.Queue(
            self,
            f"{name}Queue",
            enforce_ssl=True,
            # At least the consuming function's own timeout. Set lower, a message becomes
            # visible again while its consumer is still working and picks up a second
            # consumer -- for the provisioner's `CREATE` that is a second
            # `CreateAgentRuntime` for a configuration already being created.
            visibility_timeout=visibility,
            dead_letter_queue=sqs.DeadLetterQueue(
                queue=dlq,
                # 3, matching the `RedrivePolicy` `tests/conftest.py::_queue_with_dlq`
                # builds. `provisioner` re-enqueues an IAM-propagation retry as a *new*
                # message (invariant 15) precisely so a healthy configuration never
                # spends this budget, which leaves all three deliveries for real
                # failures.
                max_receive_count=3,
            ),
        )
        return queue, dlq
