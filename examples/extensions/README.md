# Extension Templates

These files are copyable starting points for the main Linch extension seams.
They are deliberately small, runnable, and dependency-free.

| File | Seam |
|---|---|
| `provider_template.py` | `BaseProvider` implementation |
| `memory_store_template.py` | `MemoryStore` duck-typed adapter |
| `filesystem_backend_template.py` | virtual `FileBackend` adapter |
| `tool_package_template.py` | custom tool package + registry factory |
| `hook_package_template.py` | hook package using dispatcher method names |

Use these as templates, not as shared application infrastructure. Replace the
in-memory data structures with your SDK/client/database while keeping the
method signatures and return shapes.

