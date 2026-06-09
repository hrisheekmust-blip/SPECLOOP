# Chain A composition proof via Assume-Guarantee (sound, gated)
Stubs = same interface, free (anyseq) outputs constrained ONLY by each block's PROVEN contract.
Gates: assume_sat=FAIL (satisfiable) | cover=True (beat flows) | assert_chk=FAIL (checked).

## Result: 5/7 with existing block contracts; 7/7 after adding 2 (true, inline-fallback-proven) contracts
| property | verdict | contract assumed (cited) |
|---|---|---|
| reset -> output valid low | AG-PROVEN | rate_limit ap_reset_m_valid |
| reset -> input ready low  | AG-PROVEN | pipeline_register ap_reset_slave_ready |
| backpressure reg->fifo (data stable) | AG-PROVEN | pipeline_register ap_master_data_stable_{tdata,tlast,tuser} |
| backpressure fifo->fla (data stable) | gap -> closed | fifo had NO interface contract; inline-fallback proved axis_fifo data-stable (PASS) -> add to fifo spec |
| backpressure fla->rl (data stable)   | AG-PROVEN | frame_length_adjust ap_master_data_stable |
| backpressure rl->out (data stable)   | AG-PROVEN | rate_limit ap_master_data_stable_tdata / ap_output_reg_stable_tdata |
| backpressure valid-held              | gap -> closed | no block proved m_axis valid-held; inline-fallback proved axis_rate_limit valid-held (PASS); stored ap_master_valid_stable was mis-formulated |

## Fixed the change-#3 false back-pressure property
Old (wrong): !s_axis_tready -> (m_axis_tvalid && !m_axis_tready) [conflated input vs output backpressure].
Correct (AG-proven): per boundary, $past(producer_valid) && !$past(producer_ready) -> producer data stable (+ valid held).

## Not AG-reachable from interface contracts
End-to-end data conservation ("each beat consumed exactly once through the chain") — the stubs have free
outputs uncorrelated with inputs, so no beat-count relation holds. Needs per-block conservation contracts
(count-in == count-out) or an inline scoreboard. Per-boundary "delivered intact" IS covered (data-stable G4).
