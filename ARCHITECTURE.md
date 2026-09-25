# L3A Architecture Record

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử K4 L3A.

## 1. System overview

Luồng xử lý từ `inputs/<case_id>.json` qua MCP Evidence Gateway, các Specialist Agents, Verifier đến output và trace:

```text
                          ┌─────────────────────────────┐
                          │     inputs/<case_id>.json   │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                             ┌───────────────────────┐
                             │   Coordinator Agent   │ ───► Trace: case_received, task_assigned
                             └───────────┬───────────┘
                                         │
                 ┌───────────────────────┼───────────────────────┐
                 ▼                       ▼                       ▼
      ┌──────────────────────┐┌──────────────────────┐┌──────────────────────┐
      │  Order/Item Agent    ││    Shipment Agent    ││ Payment/Refund Agent │
      └──────────┬───────────┘└──────────┬───────────┘└──────────┬───────────┘
                 │                       │                       │
                 └───────────────────────┼───────────────────────┘
                                         │  (Evidence Calls)
                                         ▼
                             ┌───────────────────────┐
                             │  MCP Evidence Gateway │
                             └───────────┬───────────┘
                                         │  (Authoritative Evidence)
                                         ▼
                             ┌───────────────────────┐
                             │ Policy & Adjudication │ ───► Trace: tool_result_consumed,
                             │         Agent         │             policy_decided, handoff
                             └───────────┬───────────┘
                                         │
                                         ▼
                             ┌───────────────────────┐
                             │     Verifier Agent    │ ───► Trace: verification_completed
                             └───────────┬───────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 ▼                                               ▼
     ┌───────────────────────┐                       ┌───────────────────────┐
     │ outputs/<case_id>.json│                       │   traces/trace.jsonl  │
     └───────────────────────┘                       └───────────────────────┘
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Quyền gọi Tool |
| --- | --- | --- | --- | --- |
| **Coordinator** | `case` JSON (case_id, customer_request, policy_version) | Khởi tạo quy trình, phân rã claims, phân công nhiệm vụ (`task_assigned`), điều phối handoff | Chuyển giao context cho Order, Shipment, Payment Agents | Không gọi MCP tool trực tiếp |
| **Order/Item** | `claimed_order_id`, `case_id` | Truy vấn thông tin đơn hàng, danh sách line items, trạng thái đơn, giá và phí vận chuyển | Trả về `order_data`, `items_data`, `seller_ids`, `order_evidence_refs` | `get_order`, `get_order_items`, `get_items` |
| **Shipment** | `claimed_order_id`, `case_id` | Truy vấn lịch sử vận chuyển, so sánh mốc giao hàng (`shipping_limit_date`, `delivered_carrier_date`, `delivered_customer_date`, `estimated_delivery_date`) | Trả về `shipment_data`, `carrier_id`, `shipment_evidence_refs` | `get_order_shipment`, `get_shipment` |
| **Payment/Refund** | `claimed_order_id`, `case_id` | Kiểm tra các bản ghi thanh toán, phát hiện giao dịch trừ trùng (`duplicate_charge`), thanh toán tách thẻ (`split_payment`), trạng thái hoàn tiền (`refund_status`) | Trả về `payments_data`, `refund_data`, `payment_evidence_refs` | `get_order_payments`, `get_payments`, `get_refund_status`, `get_refund` |
| **Policy** | Kết quả điều tra tổng hợp từ 3 Specialists, `policy_version` | Tra cứu điều khoản chính sách (`EC_POLICY_V1`), đối chiếu sự kiện thực tế với khiếu nại của khách, xác định `primary_issue`, nguyên nhân cốt lõi, trách nhiệm, số tiền hoàn | Trả về bản thảo phân định (`draft_resolution`), emit `policy_decided` | `get_policy` |
| **Verifier** | Bản thảo phân định, danh sách thực thể, tập hợp `evidence_refs` | Kiểm tra tính bất biến (invariants), đối soát tính toán số tiền hoàn, kiểm tra schema JSON công khai | Chuyển kết quả hoàn chỉnh để Coordinator ghi file và emit `case_finalized` | Không gọi MCP tool |

## 3. A2A protocol

- **Message Envelope & Correlation**:
  - Mọi trao đổi và chuyển giao giữa các Agent đều mang `case_id` làm correlation ID bất biến.
  - Sử dụng các sự kiện chuẩn: `task_assigned`, `tool_result_consumed`, `policy_decided`, `handoff`, `verification_completed`.
- **Handoff Conditions**:
  - Coordinator bàn giao song song hoặc tuần tự cho 3 Specialists điều tra thực địa.
  - Sau khi các Specialist hoàn tất thu thập bằng chứng từ MCP, dữ liệu được handoff tập trung về Policy Agent.
  - Policy Agent sau khi tổng hợp quyết định sẽ handoff bản thảo sang Verifier Agent.
- **Tránh Vòng Lặp & Timeout**:
  - Luồng xử lý là Directed Acyclic Graph (DAG) một chiều: Input → Specialists → Policy → Verifier → Output.
  - Không có vòng lặp phản hồi ngược (feedback loop) lặp vô hạn. Mỗi tool chỉ được gọi theo nhu cầu điều tra của case.

## 4. Evidence lifecycle

- **Validation**: Mọi phản hồi từ MCP Gateway được validate thông qua `Contracts.validate_evidence` theo schema `day09-mcp-evidence-v1`.
- **Lưu trữ & Mapping**:
  - `evidence_ref` được trích xuất từ phản hồi MCP và lưu trữ theo từng domain (`order`, `shipment`, `payment`, `policy`).
  - Toàn bộ `evidence_ref` duy nhất được đưa vào mảng `evidence_refs` cấp case và gắn kết vào từng `claim_assessments`.
- **Ghi nhận Trace**:
  - Ngay khi một Specialist tiếp nhận và sử dụng kết quả từ MCP, phát sinh sự kiện `tool_result_consumed` với `actor`, `tool_name`, và `evidence_refs=[ref]`.
- **Cách ly giữa các Case**: Không lưu trữ hoặc chia sẻ `evidence_ref` giữa các case khác nhau; bộ nhớ investigator được tạo mới riêng biệt cho từng case.

## 5. Failure policy

| Tình huống lỗi | Retry? | Cơ chế Fallback | Trace event / Decision code |
| --- | --- | --- | --- |
| MCP Gateway timeout | Có (tối đa 2 lần với backoff) | Ghi nhận lỗi kết nối, tiếp tục luồng với dữ liệu telemetry đã có | `tool_result_consumed` / `GATEWAY_TIMEOUT` |
| Tool trả về Not Found / Empty | Không retry | Đánh giá là thiếu dữ liệu ở domain đó; không suy đoán hoặc tạo ref giả | `attributes: {"status": "not_found"}` |
| Xung đột nguồn tin (Data Conflict) | Không retry | Ưu tiên dữ liệu telemetry từ MCP thay vì tin nhắn khai báo của khách; ghi nhận vào `data_conflicts` | `data_conflicts` / `AUTHORITATIVE_MCP_RECORD` |
| Kết quả Specialist không hợp lệ | Có (1 lần) | Verifier tự động điều chỉnh số tiền hoàn về 0 nếu không có căn cứ hoặc chuyển `case_status` về `needs_investigation` | `verification_completed` / `INVARIANT_REPAIRED` |

## 6. Verification invariants

Trước khi hoàn tất và ghi ra file `outputs/<case_id>.json`, Verifier kiểm tra bắt buộc 7 điều kiện bất biến:
1. **Schema Validation**: Output bắt buộc thỏa mãn 100% schema `day09-l3a-output-v2.schema.json`.
2. **Case ID Matching**: `output["case_id"] == case["case_id"]`.
3. **Financial Consistency**: Tổng số tiền trong các dòng `refund_lines` phải bằng chính xác `recommended_refund_brl` (sai số <= 0.01 BRL).
4. **Status & Financial Alignment**:
   - Nếu `recommended_refund_brl > 0` thì `case_status` bắt buộc phải là `"action_required"`.
   - Nếu `recommended_refund_brl == 0` thì `case_status` phải là `"no_action"` hoặc `"needs_investigation"`.
5. **Entity Scope & Evidence Provenance**:
   - Tất cả `evidence_refs` trong `claim_assessments` phải là tập con của `output["evidence_refs"]`.
   - Toàn bộ `evidence_refs` phải bắt đầu bằng tiền tố `ev_` hợp lệ được cấp từ MCP Gateway.
6. **Unique Actions**: Mảng `resolution_actions` không chứa các phần tử trùng lặp.
7. **Calibrated Confidence**: Confidence nằm trong khoảng `[0.0, 1.0]`, phản ánh đúng mức độ đầy đủ của bằng chứng thu thập được.

## 7. Reproducibility

- **Môi trường**: Python 3.11+, hệ điều hành Windows / Linux.
- **Thư viện chính**: `mcp`, `httpx2`, `jsonschema`, `referencing`, `python-dotenv`.
- **Lệnh chạy thực thi toàn bộ**:
  ```bash
  day09 run
  ```
- **Lệnh kiểm tra hợp lệ**:
  ```bash
  day09 validate
  ```
- **Lệnh đóng gói nộp bài**:
  ```bash
  day09 package --output dist/submission.zip
  ```
- **Bảo mật**: Không ghi mã API key hoặc thông tin bí mật trong source code, trace hay tài liệu kiến trúc.
