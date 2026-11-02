# Skill

[Bản gốc tiếng Anh](../../usage/skills.md)

Dùng skill khi mô hình cần gọi các quy trình prompt tái sử dụng trong một run.

[← Hướng dẫn sử dụng](./README.md)

Skill là các quy trình prompt được cung cấp cho mô hình qua tool `Skill`. Mỗi skill là một prompt có tên, có thể tái sử dụng mà mô hình có thể gọi giữa run kèm các đối số — bạn có thể hình dung chúng như những lệnh bắt đầu bằng dấu gạch chéo mà agent tự gọi cho chính mình.

---

## Bật skill

Các skill được cung cấp qua tool `Skill` khi `FeatureFlags(skills=True)`. Cờ này thuộc cấu hình tính năng của agent; bật hoặc tắt nó (cùng các phân hệ khác) qua đối số `features` của `Agent`. Xem [Cấu hình agent](./agent.md) để biết đầy đủ các tùy chọn của `FeatureFlags`.

---

## Skill `verify` tích hợp sẵn

Linch có sẵn skill `verify`:

```text
Skill({"skill": "verify", "args": "focus on billing workflow"})
```

`verify` yêu cầu mô hình lập kế hoạch và thực hiện các bước kiểm tra dựa trên bằng chứng cho công việc đã hoàn thành, rồi kết thúc bằng `VERDICT: PASS`, `VERDICT: FAIL` hoặc `VERDICT: PARTIAL`. Skill này không phụ thuộc lĩnh vực: bạn có thể dùng nó cho thay đổi phần mềm, quy trình dữ liệu, tài liệu, cấu hình hoặc các sản phẩm bàn giao cụ thể khác.

Prompt yêu cầu một dòng `VERDICT:` ở cuối mà ứng dụng chủ hoặc agent cha có thể phân tích; Linch không bắt buộc mô hình phải tuân theo. Chuỗi `args` tùy chọn giúp thu hẹp trọng tâm kiểm tra.

---

## Skill của dự án

Theo mặc định, các skill của dự án nằm tại `<cwd>/.linch/skills/<directory>/SKILL.md`. `Agent(config_dir=...)` thay đổi thư mục cấu hình; một giá trị tương đối được phân giải theo `cwd` của agent, còn đường dẫn tuyệt đối được dùng trực tiếp. Quá trình tìm kiếm quét các thư mục con trong `skills/` của thư mục đó, không quét thư mục skill trong thư mục home hay thư mục dùng chung toàn cục. Một skill hợp lệ trên đĩa có tên `verify` sẽ ghi đè skill tích hợp sẵn, không phân biệt chữ hoa chữ thường.

Mỗi tệp bắt đầu bằng phần frontmatter YAML và phần nội dung Markdown. `description` là bắt buộc; `name` mặc định là tên thư mục. Các trường tùy chọn gồm `arguments`, `argument_hint`, `when_to_use`, `allowed_tools`, `model`, `version` và `disable_model_invocation`. Tool trả phần nội dung đã thay thế giá trị về cho mô hình và ghi lại để chèn lại vào ngữ cảnh sau này. Các định nghĩa không hợp lệ và thư mục skill là symlink sẽ bị bỏ qua. Mô hình không thể gọi một skill có `disable_model_invocation: true`. Bản ghi đè của dự án cung cấp các chỉ dẫn riêng, vì vậy hãy giữ định dạng verdict trong phần nội dung nếu ứng dụng chủ của bạn phụ thuộc vào nó.

---

## Nạp lại skill

Để quét lại skill sau khi thay đổi tệp, gọi `await agent.reload_skills()`. Quá trình quét
chạy bên ngoài event loop và cập nhật tool `Skill` hiện có. Các session đang hoạt động
nhận một thông điệp nhắc về danh mục và có thể gọi các skill mới ở request tiếp theo;
phần tiền tố system ban đầu của chúng vẫn được giữ cố định. Xóa tất cả skill vẫn giữ tool
được đăng ký. Xem [tiền tố session](../architecture/system-prompt.md#tiền-tố-session-và-thay-đổi-danh-mục)
để biết các giới hạn hiện tại về khả năng lưu bền.

## Các trang liên quan

- [Cấu hình agent](./agent.md) — `FeatureFlags(skills=...)` và các công tắc bật/tắt phân hệ khác
- [Tool](../../usage/tools.md) — vị trí của tool `Skill` trong tập hợp các tool
- [Ví dụ](./examples.md) — các ví dụ có thể chạy
