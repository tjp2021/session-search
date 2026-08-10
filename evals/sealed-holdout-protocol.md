# Private sealed holdout protocol

The labeled dataset lives outside Git and is supplied through `SS_SEALED_HOLDOUT`. Git stores only its SHA-256 digest in closure evidence.

The final evaluator verifies the digest, runs once after every other gate passes, and prints aggregate counts only. Acceptance requires zero false positives and at least 95 percent recall for supported close and resume forms.

A failure remains recorded and becomes a regression. Another final attempt requires a newly labeled dataset and digest.
