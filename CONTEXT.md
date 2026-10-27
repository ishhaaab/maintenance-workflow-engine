# Domain glossary

| Term | Meaning |
| --- | --- |
| Request | A user's description of a maintenance-administration task. |
| Proposal | Model output that is either a candidate plan or a request for clarification. |
| Validated plan | A proposal checked against the request, tenant catalog, and permitted workflow. |
| Approval | Permission to execute one exact validated plan, identified by its hash. |
| Workflow | The durable progress record for one admitted request and its plan. |
| Action | One permitted external mutation: create a ticket or reserve a part. |
| Attempt | One recorded submission of an action to an external system. |
| Confirmed effect | An external mutation whose outcome has been verified. |
| Ambiguous outcome | A call may have applied an effect, but the runner cannot yet establish the outcome. |
| Reconciliation | Looking up an existing action's outcome without creating a new logical action. |
| Wakeup | A notification that a workflow may have work due; it is not the workflow state. |
| Cancellation | Preventing remaining work where permitted; it does not undo confirmed effects. |
