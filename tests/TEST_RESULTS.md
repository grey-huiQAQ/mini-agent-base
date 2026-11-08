# 测试结果记录（最新一轮全量回归 · 逐文件明细）

> **维护方式（P4-4，2026-09-12）**：下方「最新一轮全量回归」区块由 `core/test_runner.py`
> 在每次**真实全量回归**结束后自动生成并原子替换（`<!-- BEGIN/END GENERATED:test-results -->`
> 之间）——**请勿手改**，手改会在下次回归时被覆盖。
>
> - 叙述性内容（执行方式、环境说明、pytest 口径、历史沿革）统一维护在
>   `PROJECT_STATUS.md` §七，后者是**单一权威**；
> - 本文件只保留**最新一轮**的逐文件明细；更早各轮的明细见 git 历史（例如 2026-09-06 那轮
>   34 文件 / 404 用例 / 918.6s 的完整表格与「与上一记录的差异」表）；
> - 生成区块刻意**不含**子进程控制台输出（`tail`）：tail 含耗时、插件加载顺序、临时路径等
>   每次都变的内容，渲染进来会让文档 diff 永不稳定，而"diff 稳定"正是自动生成的意义。
>   需要 tail 调试请查 `test_runs` 表或 UI「🧪 测试与资源监控」Tab。

<!-- BEGIN GENERATED:test-results -->
## 最新一轮全量回归（自动生成，请勿手改）

- **执行时间**：2026-10-19 20:52:13 → 2026-10-19 21:11:43（总耗时 1170.2 秒）
- **运行编号**：`test_runs` #203
- **结果**：59 个测试文件 / **878 个用例（[OK] 878/878）**，✅ 全部通过；失败 0，超时 0

| 测试文件 | 用例 | 耗时 | 结果 |
|---|---:|---:|---|
| `test_ai_filter.py` | 24 | 23.0s | ✅ |
| `test_approval_persistence.py` | 9 | 21.8s | ✅ |
| `test_chat_content_fix.py` | 9 | 21.1s | ✅ |
| `test_concept_explainer_skill.py` | 8 | 27.0s | ✅ |
| `test_concept_quiz_skill.py` | 7 | 2.9s | ✅ |
| `test_contracts.py` | 5 | 8.3s | ✅ |
| `test_current_focus.py` | 27 | 60.0s | ✅ |
| `test_daily_planning.py` | 15 | 22.3s | ✅ |
| `test_db_backup.py` | 11 | 3.8s | ✅ |
| `test_decision_store.py` | 17 | 12.7s | ✅ |
| `test_doc_integrity.py` | 8 | 1.2s | ✅ |
| `test_eligibility_filter.py` | 44 | 29.7s | ✅ |
| `test_exemption_loader.py` | 9 | 0.4s | ✅ |
| `test_experience_feedback.py` | 10 | 4.0s | ✅ |
| `test_experience_injection.py` | 14 | 9.1s | ✅ |
| `test_experience_loop.py` | 12 | 8.7s | ✅ |
| `test_experience_store.py` | 6 | 2.4s | ✅ |
| `test_goal_features.py` | 13 | 8.6s | ✅ |
| `test_goal_learning_stats.py` | 6 | 2.7s | ✅ |
| `test_goal_source.py` | 7 | 3.2s | ✅ |
| `test_goal_stats.py` | 6 | 3.3s | ✅ |
| `test_home_dashboard.py` | 43 | 58.5s | ✅ |
| `test_idea_incubator_skill.py` | 6 | 6.9s | ✅ |
| `test_information_engine.py` | 34 | 59.3s | ✅ |
| `test_intent_agent.py` | 15 | 7.8s | ✅ |
| `test_json_utils.py` | 9 | 5.2s | ✅ |
| `test_llm_budget.py` | 6 | 4.9s | ✅ |
| `test_manual_summarize.py` | 3 | 14.5s | ✅ |
| `test_mastery_score.py` | 5 | 2.8s | ✅ |
| `test_memory_retriever.py` | 8 | 1.9s | ✅ |
| `test_model_client.py` | 8 | 3.8s | ✅ |
| `test_model_client_stream.py` | 8 | 4.2s | ✅ |
| `test_modelscope_provider.py` | 33 | 3.3s | ✅ |
| `test_note_enhancements.py` | 32 | 138.1s | ✅ |
| `test_note_store.py` | 11 | 36.2s | ✅ |
| `test_optimizations.py` | 18 | 46.9s | ✅ |
| `test_planner_milestone.py` | 13 | 11.6s | ✅ |
| `test_planning_mirror.py` | 17 | 9.7s | ✅ |
| `test_project_review_loop.py` | 32 | 17.3s | ✅ |
| `test_project_state.py` | 31 | 6.1s | ✅ |
| `test_recompute_relevance.py` | 6 | 21.7s | ✅ |
| `test_reflection_agent.py` | 9 | 8.5s | ✅ |
| `test_reminder_agent.py` | 15 | 29.1s | ✅ |
| `test_resource_log.py` | 12 | 8.7s | ✅ |
| `test_resource_scout.py` | 20 | 21.7s | ✅ |
| `test_review_recommendations.py` | 7 | 4.6s | ✅ |
| `test_review_tab.py` | 37 | 60.3s | ✅ |
| `test_role_prompts.py` | 4 | 0.8s | ✅ |
| `test_skeleton.py` | 19 | 17.6s | ✅ |
| `test_study_plan_generator_skill.py` | 8 | 3.3s | ✅ |
| `test_supervisor.py` | 21 | 4.4s | ✅ |
| `test_task_executor.py` | 14 | 32.9s | ✅ |
| `test_task_home.py` | 19 | 51.9s | ✅ |
| `test_task_lifecycle.py` | 8 | 51.2s | ✅ |
| `test_task_templates.py` | 32 | 61.4s | ✅ |
| `test_template_runs.py` | 14 | 19.6s | ✅ |
| `test_test_runner.py` | 19 | 20.6s | ✅ |
| `test_tool_permissions.py` | 5 | 5.6s | ✅ |
| `test_vector_store_sync.py` | 10 | 30.7s | ✅ |
| **合计** | **878** | **1170.2s** | ✅ 全部通过 |
<!-- END GENERATED:test-results -->
