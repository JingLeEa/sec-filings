# SQL accuracy evaluation

Does the chatbot return the result a correct SQL query returns, and does its answer say it?

- `questions.json`: 60 questions (45 the rules should handle, 15 harder ones meant for the LLM SQL writer), each with
  an answer type (count, ids, topn, groups, value, table).
- `DATA_DICTIONARY.md`: the schema and definitions the reference SQL follows.
- `gold_sql.json`: reference SQL, written by an independent model from the dictionary only (it never saw the
  chatbot's queries). Runs on the same database as the chatbot, so expected answers follow the loaded data.

```bash
python scripts/evaluate_chatbot_sql.py --mode offline --out eval/sql_accuracy/runs/offline.json
python scripts/evaluate_chatbot_sql.py --mode llm --out eval/sql_accuracy/runs/llm_1.json   # needs SoCLaaS
python scripts/score_chatbot_sql.py eval/sql_accuracy/runs/offline.json
```

Run files (`runs/`) are not committed.
