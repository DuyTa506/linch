# Các nguyên tắc phát triển

[Bản gốc tiếng Anh](../../architecture/development_principles.md)

> Thuộc [hướng dẫn kiến trúc Linch](./README.md).

Dùng trang này khi bạn rà soát phần mở rộng kernel, adapter provider, thay đổi
trạng thái bền vững hoặc đóng góp khác cho runtime.

Linch là một SDK ưu tiên async. Giữ phần lõi nhỏ gọn, trung lập với provider và an
toàn khi được nhúng cùng các agent khác trong một tiến trình. Các nguyên tắc này là
danh sách rà soát cho những điểm mở rộng mới và các thay đổi runtime.

## Phạm vi và quyền sở hữu

- Mỗi `Agent` sở hữu một `Context` riêng cho agent; khi được cung cấp một context
  của host, context đó vẫn là context cha do host sở hữu và Agent sở hữu một phạm
  vi con. Các dịch vụ dùng chung phải nằm trong phạm vi đó, không bao giờ nằm trong
  các biến toàn cục khả biến của tiến trình; truyền tường minh một phạm vi con khi
  host muốn có ranh giới riêng.
- Một `Session` sở hữu một `SessionLog` chỉ cho phép ghi thêm. `provider_view` và
  `full_history` của nó là các bản chiếu chỉ đọc, được sao chép sâu; ngữ cảnh cho
  từng yêu cầu là tạm thời và nằm ngoài nhật ký.
- Các mục `SessionLog` và dữ liệu theo dõi bản chiếu chỉ tồn tại trong bộ nhớ. Các
  thông điệp session bền vững và snapshot provider-view tùy chọn thuộc về kho
  session; nhật ký trong bộ nhớ không phải là một nhật ký bền vững thứ hai.

## Mở rộng và tác động

- Ưu tiên một điểm ghép dựa trên khả năng hoặc listener kernel hơn là thêm nhánh
  vào vòng lặp.
- Các đăng ký có thể được gỡ bỏ: `register`/`on` trả về một `Disposable` có kiểm tra
  định danh đối tượng. Await việc giải phóng; thao tác dọn dẹp chỉ được dùng một lần
  sau khi thành công và có thể thử lại sau khi thất bại.
- Các handle `next` của middleware chỉ được dùng một lần. Một wrapper phải await
  `next()` đúng một lần để chuyển tiếp, hoặc chủ ý kết thúc sớm mà không gọi nó.
- Các đường thực thi tùy chọn giữ nguyên đường thực thi trực tiếp khi không được
  sử dụng, bao gồm pipeline tool và việc điều phối hook.

## Tính tương thích và tính bền vững

- Các trường bắt buộc đã được lưu bền vững được kiểm tra nghiêm ngặt khi đọc. Chỉ
  các event được bên tạo khai báo là `ignorable` mới có thể được biểu diễn bằng
  `IgnorableEvent` và bỏ qua.
- Resume bền vững tạo dấu vân tay cho mọi ngữ nghĩa thực thi có ảnh hưởng tới chính
  sách, bao gồm `ExecutionBackend` shell+filesystem có hiệu lực và pipeline tool
  đang hoạt động. Các provider tùy chỉnh không thể xác định rõ danh tính sẽ bị từ
  chối theo nguyên tắc fail-closed thay vì phỏng đoán danh tính.
- Các listener pipeline chạy sau bước kiểm tra quyền và không thể viết lại đầu
  vào có ảnh hưởng tới việc cấp quyền; dùng hook trước tool để thực hiện phép biến
  đổi được xác thực lại.
- Các envelope được lưu bền vững yêu cầu chính xác phiên bản wire/schema hiện tại;
  trường hợp thiếu phiên bản, phiên bản trước 3.0 và phiên bản không xác định đều
  bị từ chối, vì vậy hãy bắt đầu run mới và dùng cơ sở dữ liệu mới khi kiểm tra
  phiên bản thất bại. Các backend thực thi phải cung cấp một môi trường `.shell`
  và `.fs` nhất quán; các giá trị chỉ có shell bị từ chối.

## Async và bảo mật

- Giữ các đường thực thi runtime/provider/filesystem ở dạng async; không có I/O
  chặn trong vòng lặp lượt.
- Quyền quyết định một thao tác đã được phê duyệt có được phép chạy hay không;
  các backend cung cấp khả năng quyết định nó chạy ở đâu. Không suy ra sự giới hạn
  trong sandbox từ một backend không thể quan sát rõ.
- Giữ các giao thức dùng duck typing và bề mặt `linch.__all__` được hỗ trợ; thêm tên
  công khai có chủ đích và kiểm thử các hợp đồng import cũng như tuần tự hóa của
  chúng.
