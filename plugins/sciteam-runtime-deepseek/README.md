# sciteam-runtime-deepseek

Official DeepSeek `RuntimePort` plugin for [SciTeam](https://github.com/njbinbin-piscis/sciteam).

Allows agents occupying institutional seats in SciTeam to run on DeepSeek's models
(`deepseek-chat` and `deepseek-reasoner` / DeepSeek-R1).

## Features

* **DeepSeek-Reasoner (R1) Native Support**: Automatically extracts and isolates `reasoning_content` (chain-of-thought) in `RunSpec.metadata["reasoning_content"]`, preserving pure reasoning traces for institutional audits without polluting downstream exit artifacts.
* **OpenAI-Compatible Protocol**: Connects via `https://api.deepseek.com/v1` or custom API proxies.
* **Institutional State Mapping**: Maps 401 (Auth), 402 (Insufficient Balance), 429 (Rate Limit), and 5xx into first-class `AgentOutcome` classifications (G4.1).

## Installation

```bash
pip install sciteam-runtime-deepseek
```

Once installed, SciTeam automatically discovers the plugin via standard entry points:

```bash
sciteam plugins list
```

## Configuration

Set your DeepSeek API key:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Optional configuration environment variables:
* `DEEPSEEK_MODEL`: Model name (default: `deepseek-chat`, or `deepseek-reasoner`)
* `DEEPSEEK_BASE_URL`: API endpoint (default: `https://api.deepseek.com/v1`)
* `DEEPSEEK_MAX_TOKENS`: Maximum generation tokens (default: `4096`)
* `DEEPSEEK_TEMPERATURE`: Generation temperature (default: `0.0`)

## Usage in SciTeam

```python
from sciteam.plugin_api import load_all_plugins

registry = load_all_plugins()
deepseek_factory = registry.runtime_port_factory("deepseek")
runtime = deepseek_factory()
```
