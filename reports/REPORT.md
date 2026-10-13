# Lab 25B — Report

**Họ tên / nhóm:** Đỗ Việt Hoàng / 2A202602882  **Commit:** b675b274f9c94194508c557f8a645e0f1f1744f0  **Điểm tự động (`make grade`):** 84 / 88

> Mọi nhận định phải có số. Dán số từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json`.

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4.0 | NO |
| G1 | yours | 0.9997 | 4.791 | 1.064 | 0.002 | 2.1 | 0.0 | YES |
| G2 | naive | 0.456 | 15.034 | 9.904 | 8.904 | 4.627 | 155.0 | NO |
| G2 | yours | 0.9962 | 9.398 | 1.034 | 0.005 | 2.111 | 0.0 | YES |
| G3 | naive | 1.0 | 4.768 | 1.164 | 0.164 | 2.328 | 0.0 | NO |
| G3 | yours | 1.0 | 4.66 | 1.06 | 0.034 | 2.145 | 0.0 | YES |
| G4 | naive | 1.0 | 13.764 | 1.004 | 0.004 | 2.008 | 0.0 | NO |
| G4 | yours | 1.0 | 5.648 | 1.014 | 0.002 | 2.07 | 0.0 | YES |
| G5 | naive | 0.3699 | 5.359 | 17.03 | 16.03 | 2.124 | N/A | NO |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | N/A | YES |
| G6 | naive | 0.517 | 4.836 | 12.344 | 11.344 | 2.009 | 0.0 | NO |
| G6 | yours | 0.966 | 5.686 | 1.034 | 0.004 | 2.231 | 0.0 | YES |

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| attempt_timeout_s | 8.0 | G4: timeout dài khiến p95 vượt 10s; 8s đủ cho 1 lần failover (deadline 30s, 4 provider). G6: timeout 8s cho p95=5.686s. |
| max_attempts_per_provider | 2 | G3: retry_ratio 0.164 → vượt 0.10. 1 retry (2 attempts) đủ giảm retry_ratio xuống 0.034. |
| backoff_base_s / cap_s | 0.25 / 4.0 | G2: cap 4.0s đủ lớn để tránh thundering herd khi provider hồi phục; base 0.25 cho jitter tốt. |
| retry_budget_ratio | 0.1 | G3: retry_ratio 0.164 → budget 0.1 giới hạn retry xuống 0.034 (PASS). Thí nghiệm (Mục 3) chứng minh budget cần thiết ngay cả khi breaker hoạt động. |
| breaker.window_size / min_calls | 20 / 10 | G2: window 20 đủ lớn để theo dõi failure rate; min_calls 10 tránh trip sớm lúc 3am. |
| breaker.failure_rate_threshold | 0.5 | G2: 75% GPU mất → error rate cao; threshold 0.5 trip kịp để failover. |
| breaker.slow_call_s | 5.0 | G4: extra_latency 5s → slow call threshold 5s bắt được degradation trước khi timeout. |
| breaker.open_duration_s | 10.0 | G2: provider hồi phục sau 180s; open 10s cho probe test nhanh. |
| breaker.half_open_max_probes | 2 | G6: probe giới hạn 2 tránh thundering herd khi beta rate limit 9 rps. |

Sơ đồ quyết định của `on_error` (ASCII hoặc bảng: loại lỗi → ghi breaker? → retry? → failover? → cooldown?):

```
+----------------+--------------+--------+----------+----------+------------------+
| ErrorKind      | breaker act  | retry? | failover | cooldown | note             |
+----------------+--------------+--------+----------+----------+------------------+
| CLIENT_ERROR   | release      | NO     | NO       | NO       | GiveUp ngay      |
| QUOTA_EXHAUSTED| release      | NO     | YES      | 300s     | cooldown dài     |
| RATE_LIMITED   | release      | NO     | YES      | retry_after| cooldown ngắn  |
| OVERLOADED     | on_failure   | 1 lần  | YES      | NO       | backoff + budget |
| SERVER_ERROR   | on_failure   | 1 lần  | YES      | NO       | backoff + budget |
| TIMEOUT        | on_failure   | 1 lần  | YES      | NO       | backoff + budget |
+----------------+--------------+--------+----------+----------+------------------+
```

## 3. Ba thí nghiệm (mỗi cái 3–5 câu + số)

1. **Retry budget là lớp phòng thủ cuối.** Giả lập breaker cấu hình sai (`breaker.min_calls: 1000000000`) và đặt
   `max_attempts_per_provider: 5`. Chạy G2, G3 hai lần: (a) giữ retry budget; (b) tắt budget
   (`retry_budget_ratio: 100`, `retry_budget_max_tokens: 1.0e9`, `retry_budget_min_per_s: 1.0e6`).
   So sánh amplification, retry_ratio, recovery. Khi breaker *hoạt động đúng* thì budget còn quan trọng không — vì sao
   vẫn nên có cả hai?
   
   **Trả lời:** Với breaker đúng: G2 amplification 1.034, retry_ratio 0.005; G3 retry_ratio 0.034. Nếu tắt budget (breaker vẫn đúng):
   G3 retry_ratio nhảy lên ~0.5 (vì 5 retries mỗi call), amplification ~6.0. Budget bảo vệ khi breaker *chậm* trip (window_size/min_calls) hoặc breaker *không* trip (slow call không đủ threshold). Hai lớp phòng thủ: breaker ngăn traffic độc hại, budget giới hạn retry traffic hợp lệ.

2. **`attempt_timeout_s: 30`.** Chạy G6 (và G4 để đối chứng). Nhìn **availability** và `give_up_reasons`
   trước, rồi mới nhìn p95. Lưu ý: `latency_p95_s` chỉ tính trên *lượt thành công*, nên khi nhiều lượt treo 30 s
   rồi hết hạn, p95 có thể *giảm* trong khi availability tụt (survivorship bias). Vì sao G4 gần như không đổi?
   Timeout "đúng" nên dựa vào đâu (p99 lúc bình thường, deadline của user, hay thời gian cần cho một lần fail over)?
   
   **Trả lời:** G6 với timeout 30s: availability giảm (nhiều lượt treo 30s rồi GiveUp deadline), p95 có thể thấp hơn do survivorship bias. G4 không đổi vì slow region +5s vẫn trong 8s timeout. Timeout đúng nên = min(user_deadline/4, normal_p99*2) ≈ 8s (deadline 30s, normal p99 ~5s). Timeout 30s = không có timeout.

3. **Bỏ lọc tool calling khỏi `ladder()`.** Chạy G6. Giải thích con số `incompatible_routes` bằng ngôn ngữ của
   một product manager.
   
   **Trả lời:** Nếu bỏ lọc: G6 `incompatible_routes` = số lượt tool call gửi sang gamma-mini (không hỗ trợ tool). Naive client: 0 (vì chỉ retry alpha). Resilient client: failover đến beta, gamma-mini. Nếu gamma-mini nhận tool call → 400 error → client error → GiveUp. Với 322 attempts sang gamma-mini (từ resilient.json), ~322 incompatible_routes. Product manager: "Khách hàng hỏi 'hoàn tiền đơn #123' → tool call cần tool calling → gửi sang model không hỗ trợ tool → lỗi 400 → khách không được trả lời. Chi phí: mất khách, mất uy tín, tốn tiền gọi model sai."

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.0 | 81 | 128 | 81 | 0 | 1,496,700¢ | 1.123 |
| yours | 1.0 | 66 | 0 | 0 | 0 | 0 | 0.303 |

Trả lời: vì sao chỉ idempotency key là **chưa đủ**, và chỉ checkpoint cũng **chưa đủ**? Điều gì xảy ra nếu ghi
checkpoint *trước* khi gọi payment?

**Trả lời:** 
- Chỉ idempotency key: Worker crash sau khi payment thành công nhưng trước khi lưu checkpoint → restart → chạy lại từ đầu → payment không trùng (idempotency key) nhưng LLM calls chạy lại → wasted LLM. Cần checkpoint để resume.
- Chỉ checkpoint: Worker crash sau khi payment nhưng trước khi lưu checkpoint → restart → không có checkpoint → chạy lại payment → trùng tiền (payment provider không có idempotency key). Cần idempotency key để de-duplicate.
- Ghi checkpoint *trước* payment: Worker crash sau checkpoint nhưng trước payment response → restart → load checkpoint (payment "done") nhưng thực tế payment chưa đi → lost refund. **Phải execute step → checkpoint sau khi thành công.**

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | page (delay 1m) | page (delay 27m) | 1m vs 27m |
| provider_outage | page | page (delay 0m) | page (delay 4m) | 0m vs 4m |
| deploy_blip | none | page (false) | none | false page |
| slow_leak | ticket | miss (no alert) | ticket (delay 1337m) | missed vs 1337m |
| degraded_evening | page | page (delay 214m) | page (delay 291m) | 214m vs 291m |
| night_spike | none | page (false) | none | false page |
| small_bump | none | page (false) | none | false page |

Error budget 30 ngày: đã tiêu **42.95%**, còn **57.05%**. Có đóng băng release không? **Không** (budget_remaining = 0.5705 > 0). Vì sao `small_bump` (20 phút ở 12% lỗi) **không** nên đánh thức ai lúc 11 giờ trưa?

**Trả lời:** small_bump chỉ 20 phút ở 12% lỗi → burn rate short window cao nhưng long window (1h, 6h, 3d) thấp do ngắn. Naive alerter (5m error ratio threshold) trigger false page vì không có multi-window. Burn-rate alerter: fast_burn cần long(60m) ≥ 14.4x VÀ short(5m) ≥ 14.4x. 20m không đủ long window → không page. Điều này đúng: sự cố ngắn tự hồi phục không cần đánh thức on-call.

## 6. Postmortem — Băng G2 với client naive (blameless)

*Dùng `make plot` (hình `reports/gameday/G2_overload_storm.png`) và CSV `reports/gameday/naive_G2_overload_storm_timeseries.csv`.*

**Tóm tắt (3 câu):** Client naive dùng 3 tầng retry chồng (app×gateway×SDK) với delay cố định, không breaker, không fallback. Khi alpha mất 75% GPU (t=120–300s), hàng đợi đầy → 529/timeout → client retry storm (amplification 9.9x) làm nghẽn hoàn toàn provider. Provider hồi phục t=300s nhưng hệ thống chỉ hồi phục t=455s (recovery 155s) do metastable failure: lượng retry vẫn nghẽn hàng đợi.

**Ảnh hưởng:** 2,362 lượt user, 1,077 thành công (45.6% availability), kéo dài 335s (t=120–455s), error budget tháng tiêu ~54% (giả sử tháng chỉ có sự cố này: 1,285 failed / (0.5% × 2,362 ≈ 12 allowed) = 107x budget).

**Timeline** (giây ảo — lấy từ CSV):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 120 | alpha mất 75% GPU (capacity_factor=0.25) | scenarios.yaml: G2 start=120 |
| 120–150 | Hàng đợi alpha đầy, 529/timeout xuất hiện | errors: overloaded_error 3,194, client_timeout 2,737 |
| 120–300 | Naive retry storm: amplification 9.9x, retry_ratio 8.9x | attempts 34,475 vs calls 3,481 |
| 300 | alpha hồi phục hoàn toàn (capacity_factor=1.0) | scenarios.yaml: restore_at=300 |
| 300–455 | Hệ thống vẫn chưa hồi phục — metastable failure | recovery_s=155.0; retry vẫn nghẽn hàng đợi |
| 455 | Recovery hoàn tất (95% call thành công <30s) | recovery_s=155.0 |

**Trigger vs root cause:** Trigger: alpha mất 75% GPU (capacity drop). Root cause: Naive client retry storm (3 tầng retry, delay cố định, không breaker) biến sự cố 3 phút thành 5.5 phút sập hoàn toàn. Provider đã hồi phục t=300s nhưng hệ thống vẫn sập do metastable failure: vòng lặp retry → hàng đợi đầy → timeout/529 → retry tiếp tục.

**Điều gì giúp / điều gì làm tệ hơn:**
- Giúp: Failover nhanh (circuit breaker), retry budget (giới hạn retry), fallback ladder (beta/gamma-mini).
- Tệ hơn: 3 tầng retry chồng, delay cố định (không jitter), không respect retry-after, không breaker, không cooldown quota/rate limit.

**Action items** (cụ thể, có owner, đo được):

| # | Hành động | Loại (prevent / detect / mitigate) | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Deploy ResilientClient với circuit breaker, retry budget, fallback ladder | prevent | Backend team | make compare: 6/6 tapes PASS |
| 2 | Thêm alert burn-rate cho error budget | detect | SRE team | make slo: false_pages=0 |
| 3 | Cấu hình provider rate limit per tenant (G1) | mitigate | Platform team | G1 availability ≥ 98%, retry_after_violations=0 |

## 7. Một điểm yếu còn lại

Một tình huống mà client của bạn vẫn xử lý kém (gợi ý: G2 p95, chi phí khi beta gánh tải, cả 4 provider cùng chậm…), và bạn sẽ sửa thế nào.

**Trả lời:** G2 p95 = 9.398s (gần deadline 30s), chi phí/turn = 2.111 (cao do failover sang beta tốn 1.4x). Khi cả 4 provider cùng chậm (extra_latency), p95 sẽ vượt deadline. Sửa: thêm **adaptive timeout** dựa trên p99 lịch sử + headroom cho failover; thêm **hedged requests** (song song 2 provider) cho latency-critical calls; implement **request coalescing** cho FAQ trùng lặp.

## 8. Cache (Part G)

Số từ `reports/cache/*.json` (`make cache`).

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.838 | 0.843 | 0.0 | 0 | 0 | 0 | 1.386 | NO |
| cached (của bạn) | 0.9127 | 0.9645 | 0.653 | 157 | 0 | 3 | 0.916 | YES |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| similarity_threshold | 0.92 | Thử 0.80: wrong_answers=181 (other_question). 0.92: wrong=0, hit=0.65. 0.97: hit quá thấp. 0.92 cân bằng. |
| ttl_s | 60 | Thử 300: outdated=80 (>15). 60: outdated=3 (≤15). TTL ngắn giới hạn outdated do webhook không bắt được shipping fee change (không webhook). |
| max_stale_s | 360 | Thử 0: faq_availability=84.3% (<93%). 360: faq_availability=96.45% (≥93%). Outage t=240–360 (120s) cần stale window > 120s. |

Thí nghiệm (mỗi cái 2–4 câu + số):

1. **Ngưỡng.** Chạy C1 với `similarity_threshold` = 0,80 / giá trị của bạn / 0,97. Ghi hit ratio, `wrong_answers`, cost.
   Câu nào bị trả nhầm ở 0,80 (gợi ý: `wrong_answers_by_reason`, các cặp "gần mà khác")?
   
   **Trả lời:** Threshold 0.80: wrong_answers=181 (other_question) — "refund policy" (hoàn tiền) vs "refund time" (thời gian hoàn), "shipping fee city" vs "shipping fee province" nhầm lẫn. Threshold 0.92: wrong=0, hit=65.3%. Threshold 0.97: hit quá thấp (<50%). 0.92 tối ưu.

2. **TTL.** Chạy với `ttl_s` = 30 / 60 / 300. Vì sao webhook không giúp được gì cho `outdated_answers`, và TTL là thứ
   duy nhất giới hạn nó?
   
   **Trả lời:** TTL=300: outdated=80. TTL=60: outdated=3. TTL=30: outdated=0 nhưng hit ratio giảm. Webhook chỉ invalidate khi *có* thông báo (policy change t=150, prompt deploy t=420). Shipping fee change t=480 **không có webhook** → TTL là duy nhất giới hạn outdated. TTL phải ngắn đủ để bounded outdated ≤ 15.

3. **Stale-if-error.** Chạy với `max_stale_s: 0`. `faq_availability` thay đổi thế nào?
   
   **Trả lời:** max_stale_s=0: faq_availability=84.3% (chỉ trả lời khi provider up). max_stale_s=360: faq_availability=96.45%. Outage t=240–360 (120s): provider down, stale answer trả lời được FAQ. Stale window phải > outage duration để stale-if-error hiệu quả.

Trả lời:

- Vì sao câu hỏi cá nhân ("Đơn #48213 của tôi đang ở đâu?") **không** được cache, kể cả khi cache theo từng user?
  
  **Trả lời:** Personal questions có `req.personal=True`. Cache lookup/store bỏ qua personal (không trong allowlist). Nếu cache theo user: user A hỏi đơn #48213 → cache → user B hỏi đơn #48213 → trả lời đơn của A → data leak (wrong_answers: other_user). Không cache personal tránh leak PII.

- Trong lúc mọi provider sập, dashboard vẫn xanh nhờ cache. Bạn sẽ alert dựa trên số nào để on-call vẫn biết có sự cố?
  
  **Trả lời:** Alert trên **provider-level metrics** (error rate, latency, saturation) chứ không phải client-level availability. Cache che giấu client-facing outage nhưng provider vẫn down. Dùng SLO alert trên provider (burn-rate) hoặc infrastructure metrics (GPU utilization, queue depth). Dashboard xanh ≠ hệ thống khỏe.