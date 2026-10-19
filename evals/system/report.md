# System tests (synthetic / development test copies; excluded from business aggregates)

Generated 2026-10-18 19:59:31 PDT by `python evals/system_tests.py --confirm-paid`; real API calls; total spend $0.000832. Full detail: `results.json`.

| test | outcome | evidence |
|---|---|---|
| T1_missing_text | PASS | status=quarantined reason=empty_review_text; sent to model: False |
| T2_prompt_injection | PASS | labels playback/complaint/sev 4; others: {'SYNTHETIC-0003': 'playback/complaint/2', 'SYNTHETIC-0004': 'other/complaint/2', 'SYNTHETIC-0005': 'catalog/praise/1', 'SYNTHETIC-0006': 'billing/complaint/4', '3ba33a52-9d6e-4193-b8f9-791dcba10b14': 'other/praise/1', '0ad142a3-5f2a-4a96-888c-6b87bdc2adfe': 'playback/praise/1', '98a04179-2155-44e5-ae82-4e4410ad4bcd': 'other/unclear/1', 'c26fbb2f-a7bc-4ef7-8501-2933139c1b78': 'other/praise/1', 'a099a227-8943-4b04-9b48-21d80d2d13f3': 'other/unclear/1', '4a1fa7fb-37c0-4214-bcd0-39942c008c74': 'playback/complaint/4'} |
| T3_ambiguous_unsupported | PASS | SYNTHETIC-0003: needs_review=1 (playback/complaint); SYNTHETIC-0004: needs_review=1 (other/complaint) |
| T4_temporary_api_failure | PASS | 1 failed 429 attempt(s) logged, then success; attempts [1, 2, 1] |
| T5_malformed_output | PASS | before {'pending': 12} after {'completed': 8, 'quarantined': 4}; 3 completed on retry, 4 quarantined |
| T6_planted_wrong_label | PASS | planted ['SYNTHETIC-0005', 'SYNTHETIC-0002']; detected ['SYNTHETIC-0005', 'SYNTHETIC-0002']; other flags ['0ad142a3-5f2a-4a96-888c-6b87bdc2adfe', 'a099a227-8943-4b04-9b48-21d80d2d13f3'] |

Controls (synthetic, unplanted): {'SYNTHETIC-0005': 'catalog/praise/sev 1', 'SYNTHETIC-0006': 'billing/complaint/sev 4'}
