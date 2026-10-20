---
name: hatuli-project-analysis
description: Analyze Hatuli data quality, address identity, forecast readiness and progress toward 1000 daily users using repository and measured evidence.
---

Read README.md, docs/process.md, docs/temporal_policy.md and the latest dated project reports before choosing work. Check the running server revision as well as GitHub: master can lag the deployed branch. The known server is nik@192.168.0.15, checkout /home/nik/krisha_bot; public origin https://hatuli.ai-groundtruth.com.
Measure coverage and correctness separately: a populated complex_id is not proof of a correct house. Separate canonical complex, umbrella, building and property identities. Report ambiguous addresses and source conflicts rather than increasing coverage by guessing. Use existing identity and forecast readiness code before adding another metric.
Production audits use a read-only connection, bounded queries and aggregate output. Keep private listing evidence and credentials out of Git and Notion. Distinguish observed data, hypothesis and verified improvement. Report users/day only from defined analytics with bots excluded; mark an unavailable baseline as unknown. Link tested changes and record limitations in the project log and Notion.

Audit source-name agreement before claiming catalog correctness. Check both selected cards and comparable pools: removing a bad card after calculating a median does not remove its price influence. A raw-name gate cannot detect source names overwritten with canonical labels. Separate fresh browser observations from unvisited peers and keep unresolved listing conflicts open; do not infer a verified alias from a shared address.

When auditing source refreshes, inspect dependent identities as well as the displayed name: a new source complex/address must not retain an older house or house-derived FK. Separate code-path regression, populated-house counts and independently verified physical correctness. Preventive invalidation does not mean all historical bindings or property links were reviewed.
