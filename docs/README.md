# Documentation layers

The V1 documentation has three binding layers. Lower layers may implement or
clarify higher layers, but may not silently override them.

1. [`intent/`](intent/README.md) defines why V1 exists, its scope, outcomes,
   non-goals and final success criteria.
2. [`spec/`](spec/README.md) turns that intent into observable behaviour,
   invariants, interfaces, safety constraints and architecture decisions.
3. [`implement/`](implement/README.md) records the current implementation,
   generated artifacts and engineering evidence. The executable delivery plan
   and its review history live in [`../todo/`](../todo/README.md).

When a lower layer conflicts with a higher layer, stop work and resolve the
conflict in the higher layer. Code existence never changes intent or spec.
