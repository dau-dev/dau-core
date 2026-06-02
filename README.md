# dau core

dau core ip

[![Build Status](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-core/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-core)
[![License](https://img.shields.io/github/license/dau-dev/dau-core)](https://github.com/dau-dev/dau-core)
[![PyPI](https://img.shields.io/pypi/v/dau-core.svg)](https://pypi.python.org/pypi/dau-core)

## Overview

`dau-core` owns DAU hardware-facing contracts and reusable IP:

- Register-map and protocol version models.
- Stream job trigger/status register contract for host-started aggregation jobs.
- Arrow-lite stream framing (`BatchHeader`, `ColumnDescriptor`, `OperationDescriptor`).
- Capability and DMA constraint models.
- Golden aggregation semantics.
- Canonical HDL sources packaged as importable resources.

## Current HDL Assets

- `dau_core/hdl/dau_int32_aggregation_tile.sv`: raw INT32 valid/ready aggregation tile.
- `dau_core/hdl/dau_int32_stream_aggregation.sv`: descriptor-level stream wrapper around the tile.
- `dau_core/hdl/dau_int32_arrow_lite_stream_aggregation.sv`: packed 64-bit Arrow-lite stream reader, aggregator, result writer, and status reporter for one INT32 column and one operation.
- `dau_core/registers.py`: DAU identity/status registers plus the job-control/status offsets used to start an aggregation, publish busy/done/error state, report error codes, and expose input/result buffer locations.
- `dau_core/stream.py`: operation/logical-type contracts plus shared `StreamAggregationErrorCode` values used by simulation and HDL-facing checks.

## Stream Job Register Contract

The first hardware aggregation job contract reserves the DAU register window at `0x1000`. Hosts write input address/length, output address/length, and operation metadata, then start work by writing bit 0 of `JOB_CONTROL` at offset `0x0050`. Hardware reports `IDLE`, `BUSY`, `DONE`, and `ERROR` through `JOB_STATUS` at `0x0054`; `LAST_ERROR` carries the stream/tile error code. The default staging layout uses input buffer base `0x00000000`, output/result buffer base `0x00100000`, 1 MiB for each staging region, and 136 bytes for the current packed Arrow-lite scalar result stream.

## Verification Surface

- `dau_core/tests/test_stream.py`: stream framing and error-code contract checks.
- `dau_core/tests/test_aggregation_tile_hdl.py`: packaged HDL text-contract checks.
- `dau-sim` consumes these contracts in real cocotb-on-Verilator benches and standalone Verilator SystemVerilog benches.

## Near-Term Direction

- Keep contracts versioned and explicit while growing from packed stream simulation toward DDR/register integration.
- Ensure stream error-code behavior remains synchronized across HDL, `dau-core` models, and `dau-sim` tests.

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).
