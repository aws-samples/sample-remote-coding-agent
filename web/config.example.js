// The contract `web/config.js` has to satisfy, with placeholders only.
//
// `make deploy` writes the real `config.js` from the deployed stack's outputs, and
// `web/.gitignore` keeps it out of the repository: it is not a secret, but it names one
// live deployment. Copy this file to `config.js` by hand only for a local smoke test.
//
// `apiBase` is same-origin behind CloudFront, which forwards `/api/*` to the HTTP API,
// so there is no CORS anywhere and nothing here is an absolute URL except the Cognito
// hosted-UI domain.
window.RCA_CONFIG = {
  apiBase: "/api",
  region: "PLACEHOLDER",
  userPoolId: "PLACEHOLDER",
  clientId: "PLACEHOLDER",
  cognitoDomain: "PLACEHOLDER",
};
