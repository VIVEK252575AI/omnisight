# OmniSight Frontend ↔ LLM Integration

Today's frontend change connects Live mode to the backend's safe finding/patch-proposal workflow.

- Reason stage: `GET /api/v1/findings/{finding_id}`
- Fix stage: `POST /api/v1/findings/{finding_id}/patch-proposals`
- Proposal refresh: `GET /api/v1/findings/{finding_id}/patch-proposals`
- The frontend never stores or sends the Gemini/API key. Provider credentials remain server-side.
- The generated backend patch is shown in the existing Generated patch panel when a diff is present.
- The JSON report now includes the backend patch proposal.

Backend configuration determines whether patch generation uses MOCK or Gemini. For Gemini, configure the backend environment as documented by the project; do not put provider keys in frontend files.
