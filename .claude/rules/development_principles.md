# Development Principles Reference

Reference for SOLID, KISS, YAGNI, and DRY. Use during **implementation**, **refactoring**, **design decisions**, class design, and function extraction.

**When to apply:** responsibility too broad, duplicate code, excessive complexity, design review ("what do you think of this design?"), explicit refactor requests.

**When NOT to apply:** simple bug fixes or minor one-line changes where debating principles adds no value.

---

## SOLID Principles

### S — Single Responsibility Principle

A class or module should have only one responsibility.

- One class/function = one responsibility.
- If there are multiple reasons to change, consider splitting it.
- Limits the scope of impact during changes and minimizes the ripple effect of bugs.

### O — Open-Closed Principle

Software entities should be open for extension but closed for modification.

- Design so that new features can be added without altering existing code.
- Utilize interfaces and abstract classes.
- Prevents bugs from occurring in places where the existing class is used.

### L — Liskov Substitution Principle

Instances of a superclass should be replaceable with instances of its subclasses.

- Subclasses must honor the contracts (behaviors) of their superclass.
- Maintain consistency by ensuring subclasses provide equivalent results.
- Guarantees that substitution will not break existing behavior.

### I — Interface Segregation Principle

Clients should not be forced to depend on methods they do not use.

- Split large interfaces into smaller, more specialized ones.
- Eliminate dependencies on unnecessary methods to avoid unexpected bugs.
- Design so clients only need to implement the methods they actually require.

### D — Dependency Inversion Principle

High-level modules should not depend on low-level modules. Both should depend on abstractions.

- Depend on interfaces/abstractions rather than concrete classes.
- Reduces the degree of coupling between high-level and low-level modules.
- Improves testability and ease of substitution.

---

## KISS Principle (Keep It Simple, Stupid)

Code should be kept as simple as possible.

- Always ask: "What is the simplest implementation required for this to work?"
- Complex code is prone to bugs and difficult to maintain.
- Avoid over-abstraction and over-generalization.
- Write code that is easy for the reader to understand.

---

## YAGNI Principle (You Aren't Gonna Need It)

Only add code that is needed right now.

- Do not implement features in advance just because they "might be needed in the future."
- Preemptive preparations introduce unnecessary complexity.
- Prioritize simplicity over generality; implement only currently required features.
- Implement features only when they actually become necessary.

---

## DRY Principle (Don't Repeat Yourself)

Avoid duplication of the exact same code.

- Duplicated code leads to an increased codebase size, longer modification times, and difficulty in understanding.
- Extract cohesive blocks of logic into functions or methods.
- Avoid over-applying DRY: do not forcefully consolidate similar code if the contexts or business logic are different.
- Consider consolidating when duplication occurs three or more times (Rule of Three).
