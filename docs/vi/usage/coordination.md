# Phối hợp
[Bản gốc tiếng Anh](../../usage/coordination.md)

Dùng cơ chế phối hợp qua hộp thư khi các agent cần trao đổi tin nhắn ngang hàng qua nhiều lượt.

`linch.coordination` cung cấp hộp thư trong tiến trình cho tin nhắn ngang hàng, được bật khi cần.
Ứng dụng chủ quyết định ý nghĩa của các tin nhắn ngang hàng.

Công việc lặp lại có khả năng lưu bền sử dụng `linch.worker` và PgQueuer; xem
[Mở rộng quy mô](./production.md#mở-rộng-theo-chiều-ngang).

> Các tên liên quan đến hộp thư được xuất lại từ package cấp cao nhất. Đường dẫn
> `linch.coordination.*` dùng để tổ chức nội bộ, không phải một bề mặt import
> riêng.

Để *tự triển khai* các điểm tích hợp này (kho lưu trữ bền vững, backend cô lập bằng git-worktree,
hoặc hộp thư Redis), xem [Mở rộng](./extending.md).

---

## Hộp thư — nhóm nhiều agent

Mô hình subagent cốt lõi là cha→con: một worker chạy một lần và trả về
kết luận. `linch.coordination.mailbox` bổ sung nửa còn lại — **các agent ngang hàng gửi tin nhắn
cho nhau** và trao đổi tin nhắn qua nhiều lượt. Bật bằng plugin hộp thư;
tool `send_message` được đăng ký:

```python
from linch import Agent, InMemoryMailbox, MailboxPlugin

box = InMemoryMailbox()
agent = Agent(model="gpt-5")
await agent.use(MailboxPlugin(), box)   # registers send_message and inbox
```

Hai cơ chế:

**Bus tin nhắn.** Một session có `mailbox_address` nhận nội dung hộp thư đến ở đầu
mỗi lượt (qua cùng điểm tích hợp thông báo cho lượt tiếp theo). Một agent gửi tin nhắn cho agent ngang hàng bằng
`send_message(to=..., content=..., type=...)`; agent nhận sẽ nhận tin nhắn tại ranh giới lượt đủ điều kiện tiếp theo,
trong `session.run()` hiện tại nếu còn lượt,
hoặc trong một run sau đó, dưới dạng `UserEvent` chứa `<peer-message>`. Worker được tạo dùng
địa chỉ hộp thư duy nhất khi agent có hộp thư: ID worker do
tool Subagent cung cấp, hoặc ID session con khi gọi runner trực tiếp. Tên hiển thị
chỉ là nhãn.

```python
lead = await agent.session();   lead.mailbox_address = "lead"
worker = await agent.session(); worker.mailbox_address = "alice"
# lead's turn calls send_message(to="alice", ...) → alice receives it at the next
# eligible turn boundary, in this run or a later one
```

**Giao thức yêu cầu/phản hồi.** `Correlator` là máy trạng thái từ đang chờ→đã giải quyết,
được định danh bằng `request_id`. Nó **được thiết kế để không chặn** — một agent hoạt động theo lượt không thể
chặn để chờ agent ngang hàng, nên nó mở yêu cầu, tiếp tục và kiểm tra sau:

```python
from linch import Correlator, MailboxMessage

c = Correlator()
c.open("req-001")                                    # requester registers
await box.send(MailboxMessage(sender="alice", recipient="lead",
                              content="Approve my plan?", request_id="req-001"))
# ... responder replies, echoing the id in in_reply_to ...
c.resolve(MailboxMessage(sender="lead", recipient="alice",
                         content="Approved", in_reply_to="req-001"))
assert c.is_resolved("req-001")                      # requester checks on a later turn
```

`InMemoryMailbox` là hộp thư tích hợp sẵn duy nhất. Tin nhắn tồn tại trong
vòng đời tiến trình Python hiện tại, và các agent, session, worker có thể dùng chung đối tượng hộp thư;
tin nhắn không tồn tại sau khi khởi động lại và không được chuyển qua các tiến trình. Ứng dụng chủ cần
hạ tầng bên ngoài có thể triển khai protocol `Mailbox` — xem
[Mở rộng](./extending.md).

Khi bật `DurabilityOptions(durable_inbox=True)`, hộp thư triển khai
`ClaimableMailbox` dùng lease thay cho việc lấy và xóa tin nhắn: nhận quyền xử lý → đưa vào hàng đợi bằng
`mailbox:<message-id>` → commit hộp thư đến/nhật ký trực tiếp → xác nhận. Nếu sự cố xảy ra trước
khi xác nhận thì có thể nhận lại quyền xử lý; nếu sự cố xảy ra sau khi đưa vào hàng đợi thì biên nhận trong
hộp thư đến của session giúp loại bỏ trùng lặp. Kho session cũng phải cung cấp các phương thức hộp thư đến
bền vững. Khi không bật chế độ hộp thư đến bền vững, việc chuyển tin nhắn dùng `send()` và
`drain()` có xóa tin nhắn.

Điểm tích hợp hộp thư đến chung của Context phân biệt các giá trị `Message` chưa commit với
các giá trị `Event` đã commit vào lịch sử. Đăng ký các nguồn sinh dữ liệu trên
context của agent thông qua `ctx.inbox(...)` do plugin sở hữu; vòng lặp tiêu thụ chúng tại
cùng ranh giới lượt tiếp theo như thông báo, giữ nguyên thứ tự đăng ký trong
từng context.

**Khi nào nên dùng:** các thành viên nhóm tồn tại lâu dài cần thương lượng (phê duyệt kế hoạch,
tắt có kiểm soát) hoặc tự tổ chức. Với việc ủy thác một lần, mô hình
subagent cha→con thông thường đơn giản hơn — hãy dùng mô hình đó.

Ví dụ có thể chạy: [`examples/coordination/team_mailbox.py`](../../../examples/coordination/team_mailbox.py).

---

Quay lại [Mục lục hướng dẫn sử dụng](./README.md).
