# Knowledge base

Drop the agent's source material here as `.md` or `.txt` files, then run:

```bash
python backend/rag.py
```

Suggested files:

- `resume.md` — your resume / CV in plain text or markdown.
- `projects.md` — descriptions of your GitHub projects (what, why, stack, impact).

These get chunked, embedded with an NVIDIA NIM embedding model, and stored in
the local ChromaDB collection the agent queries on every turn.
