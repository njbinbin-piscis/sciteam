---
id: mem_missing_module_offline
title: Missing Python module
scope: tool:bash
when: "ModuleNotFoundError: No module named"
author: platform
---
The sandbox has no network access, so pip install fails. Write the code with the modules already installed (`pip list` shows them).
