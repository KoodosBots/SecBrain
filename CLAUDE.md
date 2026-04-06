

<!-- SECBRAIN:START -->
## SecBrain Memory
VAULT_PROJECT=secbrain

At session START: `vault_start_session` runs automatically via hook.

### Log as you work
- Bug/error -> `vault_log_bug(project, title, description, severity, file_path?)`
- Feature implemented -> `vault_log_feature(project, title, description, status?)`
- Work item -> `vault_log_task(project, title, description, status, assignee?)`
  - status: todo | doing | review | done
  - Update as it progresses: `vault_update_task(project, title, new_status, note?)`
- Decision -> `vault_log_decision(project, title, rationale)`
- Reusable snippet -> `vault_log_code_example(project, description, code, language?, tags?)`

At session END: call `vault_end_session(project, summary)`.

### Before complex tasks
- `vault_recall_skill(query, project)` — check if this problem was solved before
- `vault_search_code(project, query)` — check saved code examples
- `vault_search_docs(project, query)` — search ingested documentation
- `vault_recall(project, query)` — broad search across everything

### Ingest reference material
- `vault_ingest_url(url, project, title?, tags?)` — fetch and store a doc page
- `vault_ingest_file(file_path, project, title?, tags?)` — store local file (.md/.txt/.pdf)

### Self-improvement
After solving something non-obvious:
- `vault_save_skill(project, title, problem, solution, code_example?, tags?, scope?)`
  - scope="global" if cross-project, "project" if specific

When a skill improves:
- `vault_improve_skill(project, skill_name, refinement)`

When noticing a user pattern:
- `vault_update_user_model(project, observation)`

### Skill quality bar
Only save skills that are: non-obvious, reusable, proven.
<!-- SECBRAIN:END -->
