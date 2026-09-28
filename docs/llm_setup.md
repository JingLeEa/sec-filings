# SoCLaaS LLM setup

The LLM client reads `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY`, and `SOCLAAS_MODEL`
from `~/.config/soclaas/soclaas.env`. Keep the actual key outside the repository.
Use directory permissions `700` and file permissions `600`.

The file is parsed directly, so IDE launches do not depend on shell startup.
Exported environment variables override file values. The default file path does
not depend on your working directory. An alternative file can be selected with
`--env-file /path/to/file.env`; a project-root `.env` is not loaded automatically.
The repository's `.env.example` contains placeholders only.

Create a project environment and install dependencies if needed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

In your IDE, select `.venv/bin/python` as the Python interpreter.

Check settings without contacting the API:

```bash
.venv/bin/python scripts/test_llm.py --check-config
```

Send one small connection test:

```bash
.venv/bin/python scripts/test_llm.py
```

Send a custom prompt:

```bash
.venv/bin/python scripts/test_llm.py --prompt "Explain a 10-K filing in one sentence."
```

The client uses SoCLaaS's configured model (`default` in the downloaded file)
through its Chat Completions endpoint. Each invocation sends one request, with
automatic retries disabled, a 60-second timeout, and a 256-token output limit.
Use `--timeout` or `--max-tokens` to change those limits. A truncated or empty
reply is reported as an error. API errors are displayed without raw response
bodies or credentials.

For interactive zsh terminals, `~/.zshrc` can also load the env file. If you
rotate the key after opening a terminal, reload that shell configuration or open
a new terminal so exported values do not override the new file with an old key.

The existing extraction and comparison pipeline does not make LLM calls. Import
`sec_disclosure.llm.client.complete` when adding an LLM analysis step later.

References: [SoCLaaS API](https://soclaas-api.comp.nus.edu.sg/),
[official OpenAI documentation](https://developers.openai.com/api/docs/libraries),
and [python-dotenv](https://pypi.org/project/python-dotenv/).
