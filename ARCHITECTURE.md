# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý bắt đầu từ `inputs/<case_id>.json` và đi qua các lớp sau:

```text
Input case
  ↓
Coordinator
  ├─ determine claimed order_id + claims
  ├─ route to specialist agents
  │   ├─ Order/Item agent -> get_order / get_order_items
  │   ├─ Payment agent -> get_payment
  │   ├─ Shipment agent -> get_shipment
  │   ├─ Policy agent -> get_policy
  │   └─ Verifier -> validate claim/evidence/schema
  ↓
MCP Evidence Gateway
  ↓
Evidence validation + evidence_ref retention
  ↓
Output JSON + trace.jsonl
```

Mỗi case được xử lý độc lập theo `case_id`. Không có shared state giữa case; evidence phải thuộc chính case đang xử lý và phải được truyền qua `evidence_ref` được server cấp. Sau khi agent thu thập đủ evidence, coordinator rút ra `primary_issue`, `claim_assessments`, `financial_resolution` và `resolution_actions`, rồi verifier chạy kiểm tra schema / logical consistency trước khi finalize.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case metadata + customer request | định nghĩa scope case, chọn order_id mục tiêu, phát phân công, điều phối handoff | task assignment + final decision sign-off |
| Order/Item agent | order_id + case_id | đọc order status, line items, seller mapping, entity IDs; chỉ gọi tool về order/item/seller nếu cần | normalized order/item/seller facts, evidence_refs |
| Payment agent | order_id + case_id | đối soát số tiền thanh toán, refund, payment reference, mismatch | payment facts + refund evidence |
| Shipment agent | order_id + case_id | xác minh trạng thái vận chuyển, ETA, delay, logistics responsibility | shipment facts + timing evidence |
| Policy agent | case policy version + claims | xác định policy rule, refund eligibility, allowed action | policy decision code + supported/unsupported verdict |
| Verifier | all evidence + tentative output | validate schema, consistent IDs, money totals, evidence ownership, claim linkage, confidence bounds | final output or rejection |

Quy tắc quyền truy vấn tool:
- Coordinator không gọi tool trực tiếp cho dữ liệu nghiệp vụ; coordinator chỉ phát nhiệm và ghi trace.
- Order/item agent được phép truy vấn order/item/seller liên quan.
- Payment agent chỉ dùng payment tool và không thay hạng mục khác.
- Shipment agent chỉ dùng shipment tool.
- Policy agent chỉ dùng policy tool.
- Verifier không gọi tool để “bù” evidence thiếu; verifier chỉ kiểm tra tính hợp lệ của output và trace.

## 3. A2A protocol

Hệ thống theo mô hình A2A (agent-to-agent) nhưng ở mức tối thiểu: message body chỉ chứa metadata quan sát được, không chứa prompt, chain-of-thought hay reasoning nội bộ.

Mỗi handoff có dạng:

```json
{
  "case_id": "L3A_CASE_001",
  "actor": "order-agent",
  "target": "payment-agent",
  "decision_code": "ORDER_RETRIEVED",
  "attributes": {"order_id": "...", "domain": "order"}
}
```

Các nguyên tắc:
- correlation key duy nhất là `case_id` và `event_id` trong trace;
- handoff chỉ xảy ra khi có ít nhất 1 evidence_ref hợp lệ tương ứng với domain;
- timeout cố định theo client HTTP (`httpx2.Timeout(..., connect=30s, write=30s, pool=30s)` và overall 300s) để tránh treo vô hạn;
- tránh vòng lặp bằng cách: mỗi domain chỉ được gọi tối đa 1-2 lần, và mỗi handoff/decision được emit xong rồi mới chuyển tiếp;
- nếu không có evidence đủ, workflow kết thúc bằng `insufficient_evidence` thay vì suy đoán.

Chỉ trace những sự kiện có thể quan sát từ bên ngoài: `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`, `case_finalized`.

## 4. Evidence lifecycle

Quy trình evidence diễn ra như sau:

1. Coordinator xác định `order_id` và list claims từ request.
2. Specialist agent gọi MCP tool với payload chứa `case_id` và đúng `order_id`/domain key.
3. Gateway gọi `ClientSession.call_tool`, validate response bằng schema `mcp-evidence-response-v1.schema.json`.
4. Mỗi MCP response phải có `schema_version`, `evidence_ref`, `result_hash`, `domain`, và `data`.
5. `evidence_ref` được lưu trong `trace` như `tool_result_consumed` kèm `tool_name`.
6. Evidence được map vào claim/output bằng cách:
   - `order` -> `affected_entities.order_ids`, `root_cause_analysis`, `claim_assessments`
   - `payment` -> `financial_resolution`, `data_conflicts`, refund logic
   - `shipment` -> `root_cause_analysis`, responsible party, delay cases
   - `policy` -> `claim_assessments[].verdict` và `resolution_actions`
7. Trước khi finalize, verifier kiểm tra từng `evidence_ref` có tồn tại trong trace của case hiện tại và không vượt quá scope case.
8. Không tái sử dụng evidence giữa các case; mỗi case được xử lý trên một dataset riêng và `case_id` phải được truyền chính xác vào mỗi API call.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có, tối đa 1 lần và idempotent | dừng domain đó, chuyển sang `insufficient_evidence` | `tool_result_consumed` không ghi nếu fail; `policy_decided` với `decision_code=insufficient_evidence` |
| Not found | Không retry nếu tool trả về resource không tồn tại | tạo output conservative, không suy đoán dữ liệu | `policy_decided` / `verification_completed` |
| Source conflict | Không retry kiểu “đổi chọn” ngẫu nhiên; đánh dấu conflict | đưa vào `data_conflicts` nếu có 2 nguồn khác nhau | `verification_completed` + `decision_code=data_conflict` |
| Invalid specialist result | Không dùng giá trị đó; yêu cầu viết lại hoặc bỏ qua | fallback về evidence còn hợp lệ, nếu không có thì `insufficient_evidence` | `verification_completed` cùng `decision_code=invalid_result` |

Giới hạn retry và idempotency:
- retry chỉ dùng cho transient network/timeout, không dùng cho conflict logic;
- nếu tool đã gọi với cùng `case_id` và cùng payload mà kết quả lỗi, hệ thống không “phối hợp” thêm dữ liệu giả;
- missing evidence không được biến thành “truth” thông qua suy diễn tự do.

## 6. Verification invariants

Trước khi finalize, verifier phải kiểm tra các invariant sau:

- Schema: output phải pass `l3a-output-v2.schema.json` và trace phải pass `trace-event-v1.schema.json`.
- Entity scope: `order_ids`, `item_ids`, `seller_ids`, `payment_references`, `shipment_ids` phải thuộc cùng case và không chứa ID từ case khác.
- Evidence ownership: mọi `evidence_ref` trong output/trace đều phải là evidence thật của case hiện tại; không dùng evidence chéo case.
- Claim linkage: `claim_assessments[].claim_id` phải khớp với `customer_request.claims` và `verdict` phải tương ứng với claim topic.
- Money totals: `financial_resolution.recommended_refund_brl` phải nhất quán với refund lines, không âm, và có logic đáng tin cậy với payment evidence.
- Responsibility/action consistency: `root_cause_analysis.responsible_parties` và `resolution_actions` phải thống nhất với `primary_issue`.
- Confidence bounds: `confidence` trong `assessment` và `claim_assessments` nằm trong `[0, 1]` và không vượt quá mức được hỗ trợ bởi evidence quality.

Nếu một trong các invariant không đạt, hệ thống phải keep conservative status (`needs_investigation` hoặc `insufficient_evidence`) thay vì hứa hẹn một quyết định không đủ evidence.

## 7. Reproducibility

Cấu hình thực thi được giữ tối thiểu và không chứa secret:

- Python: 3.11+
- dependency chính: `httpx2`, `jsonschema[format]`, `mcp`, `python-dotenv`
- concurrency: tuần tự theo case, không chạy song song multiple cases trong cùng process
- random seed: không dùng randomness cho quyết định nghiệp vụ; nếu phải tạo event_id, dùng `secrets.token_urlsafe` như trong `TraceWriter`
- lệnh chạy chính:

```bash
python -m pip install -e ".[dev]"
python -m day09 --help
python -m day09 run
python -m day09 validate
python -m day09 package --output dist/submission.zip
```

Giới hạn tài nguyên:
- mỗi output JSON dưới 1MB theo kiểm tra nộp bài;
- tổng thư mục submission dưới 12MB;
- case được xử lý 1-by-1 để dễ debug, trace rõ ràng và tránh race condition.

Không ghi API key, bearer token hoặc secret vào repo, vào output, hoặc vào trace.
