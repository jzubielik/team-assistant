# 0005. The web page: static files on S3 behind CloudFront

Status: accepted · 2026-11-15

## Context

A browser page for demo users the operator creates. The page talks to the agent exactly as
`make chat` does, through the gateway, so it needs no server of its own. The project has no
own domain, no fixed monthly fees by default, and `make destroy` must remove everything.
S3 website endpoints serve only HTTP (S3 guide, "Website endpoints").

## Decision

- **Static files in a private S3 bucket, read only by one CloudFront distribution** through
  origin access control; the `*.cloudfront.net` name with AWS's certificate. In the same stack
  as the agent, so `make destroy` removes it and Cognito and the Runtime get its address
  without a cross-stack reference. Rejected: Amplify Hosting (a further service and deployment
  model for a handful of files); S3 website hosting (no HTTPS).
- **Uploads by `aws s3 sync` in `make deploy` / `make web-sync`,** then one invalidation.
  Rejected: CDK's `BucketDeployment` (a Lambda function, a role and an AWS CLI layer to copy a
  few files). `make destroy` empties the bucket first instead of `autoDeleteObjects` (also a
  Lambda function).
- **Security headers in two places.** A CloudFront response headers policy with no
  environment-specific value (CSP without the gateway's address, HSTS, frame, referrer and
  content-type headers), and a CSP meta tag written into each HTML file at upload, whose
  `connect-src` names exactly the gateway and the login domain. Putting the addresses into the
  header would make a loop in the stack: the distribution would need the gateway, the gateway
  the Cognito client, the client the distribution's address.
- **One build for every environment:** the page reads `config.json`, written at upload from the
  stack outputs.
- **The page's own stack: TypeScript, Vite, Vitest, markdown-it,** fonts bundled (no request to
  a font service). The AWS-only rule covers agent components, not the page.
- **A fourth bootstrap policy** (`TeamAssistantCdkExecutionWeb`) for the S3 and CloudFront
  resources, with actions from the CloudFormation resource schemas.

## Consequences

- No server and no fixed fee: CloudFront and S3 bill per request, per transferred byte and
  per stored byte.
  Creating and deleting a distribution adds minutes to the first deploy and to destroy.
- The page is developed against a mock agent in the Vite development server, so AWS is needed
  only for short checks.
- A page uploaded without `make deploy` or `make web-sync` would lack the narrow meta CSP
  (docs/security.md, residual risks).
