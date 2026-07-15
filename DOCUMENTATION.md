# Technical documentation

The maintained technical and operational documentation is in [README.md](README.md),
with security policy in [SECURITY.md](SECURITY.md) and release history in
[CHANGELOG.md](CHANGELOG.md).

The August 2025 document was replaced because it described unverified accuracy,
performance, backtesting, and production capabilities that were not reproducible
from the repository.

Version 2.1 adds the documented fuzzy expert recommendation engine and optional
OpenAI second-opinion contract. Neither layer executes orders or carries a verified
accuracy or profitability claim. The JSON/Markdown audit trail records the effective
local action, optional model action/model/prompt/input hash/cited event IDs, and the
effective configured risk caps without allowing the model to alter the decision.
