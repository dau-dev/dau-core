# Registry-wide guards

The guards are `pytest` tests that walk the whole registry rather than one
core. They run with `python -m pytest dau_core/tests`; none of them needs
Vivado, and only the elaboration guard needs `verilator`.

Four of them compare an *exact set* rather than a predicate. Those are
scoped to `source_package == "dau_core.hdl"` so that an installed
third-party tile does not read as drift in this package's palette.

## `test_cores.py`

Registry shape, declaration completeness, and the resource-pricing
contract.

| Guard                                                                          | Fails on                                                                                                                                          |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_every_hdl_file_belongs_to_exactly_one_core`                              | An `.sv`/`.v` file in `dau_core/hdl/` with no entry, an entry with no file, a duplicated `source`, or rendered output shadowing a generated core. |
| `test_all_source_paths_exist`                                                  | An entry whose `source_path()` does not resolve. Generated cores are skipped.                                                                     |
| `test_goldens_resolve_to_callables`                                            | A `golden` string naming a module or attribute that does not import, or that is not callable.                                                     |
| `test_benches_are_importable_modules`                                          | A `bench` string naming a module that fails to import.                                                                                            |
| `test_every_operator_has_a_golden`                                             | An operator tile with no reference semantics.                                                                                                     |
| `test_every_selectable_operator_declares_its_ingest_rate`                      | A missing `ingest`, an unproven faster-than-1-row-per-cycle claim, or a `scales_with_param` naming an undeclared parameter.                       |
| `test_every_selectable_operator_declares_its_contract_kind`                    | An operator with no `contract_kind`, or a non-operator that declares one.                                                                         |
| `test_every_selectable_operator_declares_its_record_words`                     | An operator declaring neither or both of `record_words` and `record_words_param`, or resolving below 1.                                           |
| `test_parameterized_record_words_resolve_from_defaults_and_overrides`          | `effective_record_words` drifting for the parameterized cores.                                                                                    |
| `test_only_combinational_per_beat_cores_support_pipeline_depth`                | A stateful or multi-word tile claiming the generic elastic-depth axis.                                                                            |
| `test_wide_combinational_tiles_declare_pipeline_depth_without_changing_ingest` | The wide combinational tiles silently moving their depth bounds or their per-slot rate.                                                           |
| `test_sources_for_closes_dependencies_in_order`                                | A dependency closure that emits a package after its consumer, or duplicates a source.                                                             |
| `test_lookup_errors_are_typed`                                                 | `core()` or `core_for_module()` raising a bare `KeyError`.                                                                                        |
| `test_synthesizable_cores_carry_measured_envelopes`                            | A synthesizable operator or stream-infra tile with no envelope, an unmeasured estimate, or a zero-LUT point.                                      |
| `test_exact_resource_measurement_resolves_with_parameter_defaults`             | Coordinate lookup ignoring a declared parameter default.                                                                                          |
| `test_resource_measurement_refuses_an_unmeasured_part`                         | One part's number pricing another part's build.                                                                                                   |
| `test_every_registered_core_with_resources_carries_measurement_points`         | A resource record that is not a list of measurement points.                                                                                       |
| `test_an_unclocked_measurement_answers_for_any_clock_and_records_no_timing`    | A combinational core's point recording a timing status it never had, or failing to price a clock.                                                 |
| `test_violated_measurement_is_retrievable_but_not_a_usable_envelope`           | A timing-failed build being consumed as a smaller budget.                                                                                         |
| `test_a_measured_point_matches_a_derived_clock_period`                         | A period derived as `1000 / job_clock_mhz` failing float equality against its measured point.                                                     |
| `test_registry_is_hydra_composition_not_a_python_literal`                      | A parallel Python-literal core list reappearing, and the registry-path view drifting from the accessor view.                                      |
| `test_accessors_defer_to_a_composed_registry`                                  | A default reload clobbering a consumer's composed overrides.                                                                                      |
| `test_silent_on_success_operator_set_is_pinned`                                | A core changing its status class (silent mid-chain or a per-batch close-out) without the change being stated.                                     |
| `test_dispatcher_rate_still_needs_record_aware_modeling`                       | The known record-width-blind dispatcher rate gap being treated as closed.                                                                         |
| `test_authoritative_models_reject_impossible_values`                           | Negative resource counts, empty part strings, or non-positive clock periods entering the registry.                                                |
| `test_parameter_spec_rejects_incoherent_constraints`                           | A spec that would reject every override, including its own default.                                                                               |
| `test_parameter_spec_violation_is_the_single_constraint_source`                | The override rule being re-implemented outside `ParameterSpec.violation()`.                                                                       |
| `test_storage_intent_prices_against_declared_parameters`                       | Storage cost ignoring a parameter override.                                                                                                       |
| `test_storage_intent_refuses_an_undeclared_scale_parameter`                    | An intent scaling by a parameter the core does not declare.                                                                                       |
| `test_storage_intent_without_scale_is_a_fixed_cost`                            | A scale-free intent being mispriced.                                                                                                              |
| `test_storage_bytes_refuses_illegal_scale_overrides`                           | Pricing an override that no buildable design could use.                                                                                           |
| `test_a_core_resolves_hdl_from_the_package_that_declares_it`                   | A third-party tile being forced to vendor its SystemVerilog into `dau_core.hdl`.                                                                  |
| `test_a_well_formed_vendor_tile_does_not_trip_our_palette_guards`              | An installed third-party tile failing this package's exact-set guards.                                                                            |
| `test_a_core_naming_an_uninstalled_hdl_package_refuses_clearly`                | A raw `ImportError` where the message should name the missing distribution.                                                                       |
| `test_an_unresolvable_hdl_source_says_so`                                      | A resource path that formats correctly and does not exist, discovered at synthesis.                                                               |

## `test_core_contract.py`

| Guard                                                      | Fails on                                                                                                                                                     |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `test_every_operator_conforms_to_the_stream_tile_contract` | A `row-stream` operator whose real SystemVerilog port list drifts from the stream and status contract. Ports are parsed from the source rather than assumed. |

The check runs through `dau_build.sv_contract.validate_stream_tile`. In CI a
missing `dau_build` fails the guard; anywhere else it skips.

## `test_measured_from_matches_the_hdl.py`

| Guard                                                        | Fails on                                                                                                                                  |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `test_every_stamped_envelope_matches_its_hdl_closure`        | An edit to a tile's source, or to a package it imports or a header it includes, without re-measuring and re-stamping its `measured_from`. |
| `test_the_unstamped_envelopes_are_exactly_the_declared_ones` | A measured core without a `measured_from`, beyond the pinned unstamped set, which can only shrink.                                        |
| `test_every_core_has_a_computable_hdl_closure`               | A core whose HDL closure cannot be digested.                                                                                              |
| `test_a_stamp_without_a_measurement_is_refused`              | A `measured_from` on an entry that records no resources.                                                                                  |

## `test_hdl_elaboration.py`

| Guard                                             | Fails on                                                                                                                                                                                                        |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_every_declared_parameterization_elaborates` | The registry advertising a parameter value the RTL's own elaboration guard rejects. Parametrized per operator; a lint-only Verilator run over the declared parameter space, capped at 24 combinations per core. |

## `test_parameter_bounds_match_the_hdl.py`

| Guard                                                            | Fails on                                                                                                            |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `test_declared_bounds_match_the_tiles_elaboration_guard`         | A registry `minimum`, `maximum`, or `choices` that disagrees with the bound stated in the tile's own `$error` text. |
| `test_unparsed_guards_are_declared_and_still_exist`              | The unparsed-guard holdout list outliving its holdouts, in either direction.                                        |
| `test_the_python_mirror_of_the_top_k_bound_matches_the_registry` | `dau_core.operators.TOP_K_MAX` drifting from the registry's declared `K` maximum.                                   |

Because the bound is parsed out of the `$error` string, that string is
machine-readable text, not a comment. The recognized forms are
`X must be in [a, b]`, `X must be between a and b`,
`X must divide N and lie in [a, b]`, `X must be a/b/c`, and
`X must be a, b, or c`.

## `test_hdl_parameter_bindings_resolve.py`

| Guard                                                           | Fails on                                                                                                |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `test_every_parameter_binding_names_a_declared_parameter`       | A `.NAME()` binding in a `dau_*` instantiation naming a parameter the target module no longer declares. |
| `test_the_identity_wrapper_forwards_every_capability_parameter` | The identity wrapper failing to forward a capability parameter, which would advertise it as zero.       |

## `test_capability_contract.py`

| Guard                                                                  | Fails on                                                                                          |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `test_registry_declares_hardware_opcodes_only_for_opcode_driven_tiles` | A tile declaring an opcode it cannot execute, or declaring a host-computed operation as hardware. |

## `test_public_tile_surface.py`

| Guard                                                    | Fails on                                                                                         |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `test_the_authoring_surface_stays_extractable`           | `cores`, `design`, `generators`, or `stream` growing an import into this package's private side. |
| `test_the_surface_declares_its_third_party_dependencies` | The declaration surface acquiring a runtime dependency beyond `ccflow` and `pydantic`.           |
| `test_the_extraction_debt_does_not_grow`                 | The declared debt list growing, and separately, going stale.                                     |

Imports are read statically rather than by importing the modules, because
an import-time check passes on a dependency reached only inside a function.

## `test_benches_can_fail.py`

| Guard                                            | Fails on                                                                                                                                                    |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_a_bench_fails_when_its_tile_is_made_wrong` | A bench that always passes. Each mutation edits a copy of the HDL and asserts the bench fails, and that it failed in the *run* rather than at compile time. |

## `test_generated_cores.py`

| Guard                                                          | Fails on                                                                  |
| -------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `test_a_generated_core_refuses_its_source_until_rendered`      | A generated core silently resolving to a stale or absent file.            |
| `test_rendering_writes_the_source_the_entry_names`             | Rendered output landing somewhere the registry entry does not name.       |
| `test_a_checked_in_core_refuses_to_be_rendered`                | A checked-in source being overwritten by a render.                        |
| `test_the_operating_point_comes_from_configuration`            | A generator's operating point being fixed in Python rather than composed. |
| `test_each_operating_point_renders_a_different_source`         | Two operating points collapsing to one source.                            |
| `test_the_rendered_source_carries_the_parameters_that_made_it` | An untraceable generated file.                                            |

`dau_core/tests/conftest.py` renders the generated cores once per session
in an autouse fixture, so registry-walking guards see real files.

## See also

- [How to test a core](../how-to/test-a-core.md): the order to run these in while
  developing a tile.
- [How to add an operator tile](../how-to/add-an-operator-tile.md): the steps these
  guards check.
- [`CoreDefinition` fields](core-definition.md): the declarations most of these
  guards read.
