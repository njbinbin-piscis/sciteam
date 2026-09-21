# sciteam-runtime-anthropic

Official Anthropic Claude `RuntimePort` plugin for [SciTeam](https://github.com/njbinbin-piscis/sciteam).

Allows agents occupying institutional seats in SciTeam to be driven by Anthropic's Claude
models (e.g., `claude-3-5-sonnet-20241022`, `claude-3-7-sonnet-latest`, `claude-3-opus-20240229`).

## Installation

```bash
pip install sciteam-runtime-anthropic
```

Once installed, SciTeam automatically discovers the plugin via standard entry points:

```bash
sciteam plugins list
```

## Configuration

Set your Anthropic API key via environment variable:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Optional configuration environment variables:
* `ANTHROPIC_MODEL`: Model name (default: `claude-3-5-sonnet-20241022`)
* `ANTHROPIC_BASE_URL`: API endpoint (default: `https://api.anthropic.com/v1`)
* `ANTHROPIC_MAX_TOKENS`: Maximum generation tokens (default: `4096`)
* `ANTHROPIC_TEMPERATURE`: Generation temperature (default: `0.0`)

## Usage in SciTeam

```python
from sciteam.plugin_api import load_all_plugins

registry = load_all_plugins()
anthropic_factory = registry.runtime_port_factory("anthropic")
runtime = anthropic_factory()
```
