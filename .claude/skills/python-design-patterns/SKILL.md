---
name: python-design-patterns
description: Use when designing or refactoring Python components — deciding whether to add an abstraction, choosing composition vs inheritance, evaluating complexity and coupling, or planning modular/layered architecture. Covers KISS, SRP, rule of three, and dependency injection.
---

# Python Design Patterns

Write maintainable Python code using fundamental design principles. These patterns help you
build systems that are easy to understand, test, and modify.

## When to use this skill

- Designing new components or services
- Refactoring complex or tangled code
- Deciding whether to create an abstraction
- Choosing between inheritance and composition
- Evaluating code complexity and coupling
- Planning modular architectures

## Core concepts

### 1. KISS (Keep It Simple)

Choose the simplest solution that works. Complexity must be justified by concrete requirements.

### 2. Single Responsibility (SRP)

Each unit should have one reason to change. Separate concerns into focused components.

### 3. Composition Over Inheritance

Build behavior by combining objects, not extending classes.

### 4. Rule of Three

Wait until you have three instances before abstracting. Duplication is often better than
premature abstraction.

## Quick start

```python
# Simple beats clever
# Instead of a factory/registry pattern:
FORMATTERS = {"json": JsonFormatter, "csv": CsvFormatter}

def get_formatter(name: str) -> Formatter:
    return FORMATTERS[name]()
```

## Best practices summary

- **Keep it simple** — choose the simplest solution that works
- **Single responsibility** — each unit has one reason to change
- **Separate concerns** — distinct layers with clear purposes
- **Compose, don't inherit** — combine objects for flexibility
- **Rule of three** — wait before abstracting
- **Keep functions small** — 20–50 lines (varies by complexity), one purpose
- **Inject dependencies** — constructor injection for testability
- **Delete before abstracting** — remove dead code, then consider patterns
- **Test each layer** — isolated tests for each concern
- **Explicit over clever** — readable code beats elegant code

## Troubleshooting

**A class is growing and seems to have multiple responsibilities, but splitting it feels
wrong.** Apply the "reason to change" test: list every change that could require editing
this class. If the list has items from different domains (e.g. HTTP parsing AND business
rules AND formatting), split it. If all changes stem from the same domain concern, the class
may be appropriately sized.

**Injecting all dependencies through the constructor is producing constructors with 7+
parameters.** This is a sign of too many responsibilities in one class, not a problem with
dependency injection. Split the class into smaller units first, then each constructor
naturally becomes smaller.

**Composition is producing deeply nested wrapper objects that are hard to trace.** Keep the
composition shallow (2–3 levels). If wrapping is the only mechanism, consider whether a
Protocol-based approach or simple function composition would be cleaner than a chain of
decorator objects.

**The rule of three says not to abstract yet, but the duplication is causing bugs when one
copy is updated but not the other.** Duplication that diverges in dangerous ways should be
abstracted sooner. The rule of three is a heuristic, not a law. If the copies are already
diverging incorrectly, extract immediately and add a test that exercises the shared behavior.

**A service layer is importing from the API layer, breaking the dependency direction.** This
is a layering violation. The service layer must not import from handlers. Introduce a shared
types/models layer that both can import from, keeping the dependency arrow pointing downward
(API → Service → Repository).
