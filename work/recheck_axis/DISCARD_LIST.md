# Genuinely-WRONG assertions discarded in the gated re-proof
(real counterexamples under a sound, gate-verified AXIS environment; the modules are correct)

## Inverted skid / mutex semantics (full skid can have ready downstream; both skid/temp regs CAN be valid)
- axis_register:ap_skid_buffer_backpressure_condition
- axis_rate_limit:ap_skid_buffer_no_overflow
- axis_cobs_decode:ap_no_simultaneous_temp_valids

## Reset-polarity / combinational-reset flips (assert registered-reset behavior on inverted or combinational signals)
- axis_cobs_decode:ap_reset_s_ready (asserts TREADY HIGH in reset; module resets it LOW)
- axis_adapter:ap_reset_slave_ready (combinational bypass ready not reset-gated)

## Config-dependent claims stated unconditionally (hold only for some rate_num/rate_denom/by_frame configs)
- axis_rate_limit:ap_accumulator_bounded
- axis_rate_limit:ap_no_ready_when_paused
- axis_rate_limit:ap_ready_implies_not_paused
- axis_rate_limit:ap_rate_by_frame_pause_in_frame
- axis_rate_limit:ap_master_valid_stable

## Over-strict FSM / counter / pointer transitions (the module's real legal paths violate the claim)
- axis_fifo:ap_rd_ptr_never_exceeds_commit
- axis_cobs_encode:ap_input_count_resets_on_boundary
- axis_cobs_encode:ap_output_count_decrements
- axis_cobs_encode:ap_output_alternates_code_data
- axis_cobs_decode:ap_tlast_implies_idle
- axis_mux:ap_frame_start
- axis_mux:ap_no_ready_disabled
- axis_mux:ap_valid_needs_frame
- axis_frame_join:ap_tlast_only_on_final_port
- axis_frame_length_adjust:ap_passthrough_length
- axis_frame_length_adjust:ap_short_counter_bound
- axis_frame_length_adjust:ap_long_counter_bound
- axis_frame_length_adjust:ap_truncate_no_output
- axis_frame_length_adjust:ap_output_pipeline_temp
- axis_demux:ap_output_temp_mutex
- axis_demux:ap_tlast_propagates
- axis_demux:ap_ready_reflects_master

## Mis-specified FIFO full/empty invariants (ignore simultaneous read/write timing)
- axis_srl_fifo:ap_master_valid_when_not_empty
- axis_srl_fifo:ap_no_ready_when_full
- axis_srl_fifo:ap_no_valid_when_empty
- axis_srl_fifo:ap_no_write_when_full
- axis_srl_fifo:ap_no_read_when_empty

## Control-input stability stated as ASSERTION (mis-categorized; would hold given a control-input assumption — NOT wrong content)
- axis_demux:ap_select_stable_after_start
- axis_demux:ap_select_stable_during_frame
- axis_demux:ap_drop_stable_after_start
- axis_demux:ap_drop_stable_during_frame
