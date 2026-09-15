"""Presentation / display boundary (T012, ADR-009).

This package owns the single named display/statistical boundary
between the integer protocol domain and the ``Decimal`` display
domain. It is the only module in V1 permitted to import ``Decimal``
(see ADR-009). It must not import the RPC, storage, or execution
layers and must not be imported by the protocol/domain layer.
"""
