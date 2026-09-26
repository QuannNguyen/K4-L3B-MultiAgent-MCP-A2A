# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng dữ liệu từ input đến output tuân theo pipeline sau:

```text
Input case → Entity resolver → Coordinator → Specialist agents → Conflict resolver → Verifier → Output JSON
        │                 │                    │                    │                    │
        └─────────────────┴────────────────────┴────────────────────┴────────────────────┴────→ Trace log
                                     │
                                     └── MCP evidence calls: customer/order/shipment/payment/policy
```

Tại mỗi case, hệ thống nhận `case_id` và dữ liệu liên quan, đánh giá candidate identity, gọi tool đúng domain theo discovery, tổng hợp evidence, rồi xây ra output schema `day09-l3b-output-v2` kèm trace `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | customer ID, order IDs, candidate records | resolve customer identity and constrains candidate set | customer history, order lookup | pass normalized IDs to coordinator |
| Coordinator | case metadata + evidence summary | choose investigation path, assign tasks, stop redundant calls | discovery only; no broad tool sweep | handoff the highest-value work to specialists |
| Order/product | order IDs, item IDs, seller IDs | confirm item/order scope and decide if order is valid or cancelled | order/item lookup | produce order verdict and candidate set |
| Shipment | shipment IDs and delivery timestamps | assess late-delivery, lost shipment, or timeline completeness | shipment history/tool results | shipment verdict and timeline status |
| Payment/refund | payment refs and refund records | reconcile captures, refunds, duplicates, and recommended refund | payment/refund history | payment verdict and refund totals |
| Policy | case facts + source precedence | decide responsibility and whether evidence is sufficient | policy reference and source rules | policy decision code |
| Conflict resolver | specialist outputs | merge conflicting sources and select one authoritative source | no extra network tools beyond needed checks | final source-precedence record |
| Verifier | draft output + evidence refs + trace | validate schema, evidence ownership, and claim consistency before finalize | schema validation, no live tools | final output or rejection with corrective action |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Entity resolution bắt đầu từ `case_id` và tất cả các identifier có trong input như `customer_unique_id`, `order_id`, `seller_id`, `shipment_id`, `payment_reference`. Hệ thống loại bỏ các candidate không khớp với nhau, giữ candidate đang được case đề cập trực tiếp, và đánh dấu các candidate bị reject bằng `rejected_candidates`. Nếu chỉ có một ID rõ ràng, trạng thái là `resolved`; nếu nhiều candidate cùng khả năng, trạng thái là `ambiguous`; nếu không còn candidate sau lọc, trạng thái là `not_found`.

Mỗi handoff được thực hiện dưới envelope tối thiểu: `case_id`, `actor`, `target`, `reason`, `evidence_refs` nếu có. Không ghi chain-of-thought hoặc prompt nội bộ vào trace. Mỗi bước có `case_id` cố định để tránh cross-case contamination, và không có vòng lặp vì số lượng tool call được giới hạn ở một vài domain cốt lõi theo priority order: customer → order → shipment → payment → policy.

## 4. Evidence và conflict lifecycle

Mọi MCP response đều được validate bởi `Contracts.validate_evidence` trước khi dùng. `evidence_ref` được giữ nguyên, không tự gán, và chỉ được lưu trong trace nếu response thực sự được tiêu thụ. Hệ thống ưu tiên source phù hợp với domain và case: dữ liệu từ `order` và `payment` được coi ưu tiên hơn mô tả văn bản đơn thuần; nếu dữ liệu mâu thuẫn, conflict resolver sẽ lưu một mục trong `data_conflicts` với `field`, `sources`, `selected_source`, `resolution_code`.

Evidence lifecycle bao gồm 4 bước: discovery, fetch, validate, consume. `tool_result_consumed` chỉ emit sau khi response đã validate và được map vào output hoặc vào quyết định. Không tái sử dụng evidence giữa các case và không vượt quá số lượng tool call cần thiết cho một case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 1 retry only | return conservative insufficient-evidence result | `tool_result_consumed` with warning metadata or final `verification_completed` |
| Entity not found/ambiguous | 0 extra broad search | keep `entity_resolution` in not_found/ambiguous, low confidence | `policy_decided` with `ENTITY_AMBIGUOUS` |
| Source conflict | 0 additional fetch unless critical | select source by precedence and record conflict | `policy_decided` with `SOURCE_PRECEDENCE` |
| Invalid specialist result | 1 validation pass | discard invalid block and use coordinator fallback | `verification_completed` with failed validation trace |

Query budget được giới hạn theo domain: tối đa 1 customer call, 1 order call, 1 shipment call, 1 payment call, 1 policy call cho mỗi case, trừ khi case có dữ liệu bắt buộc hơn. Cache tạm thời ở phạm vi case để tránh gọi lặp, và không cố suy diễn dữ liệu thiếu từ fallback giả tưởng. Nếu evidence không đủ, output phải trả về `insufficient_evidence` cùng confidence thấp thay vì đoán.

## 6. Verification invariants

Trước khi finalize, verifier kiểm tra:

- schema hợp lệ với `day09-l3b-output-v2`;
- `case_id` khớp với input case;
- `entity_resolution.resolved_order_ids` không vượt phạm vi case và `rejected_candidates` được lưu đúng;
- evidence refs thuộc cùng `case_id`, cùng run và cùng team;
- source precedence và `data_conflicts` song hành với `root_cause_analysis`;
- timeline shipment và payment totals nhất quán với recommended refund;
- `confidence` nằm trong [0, 1];
- resolution actions phù hợp với `primary_issue` và `case_status`.

## 7. Reproducibility

Mô hình và cấu hình được giữ đơn giản và deterministic: Python 3.11+, package dependency được pin qua `pyproject.toml`, không cần random seed vì không có model inference. Có giới hạn concurrency thấp (một case xử lý tuần tự trong loop CLI) để dễ tái lập. Các lệnh chạy chính:

```bash
python -m pip install -e ".[dev]"
pytest -q
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không ghi API key, `.env`, hoặc nội dung nhạy cảm vào artifact. Resource limit cứng là 12 MB uncompressed cho payload submission và 1 MB tối đa cho từng file trong ZIP.