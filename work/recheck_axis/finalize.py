"""Finalize the gated library re-proof: write sound records, correct the false
all_proven flags in Qdrant, emit the grouped discard list. Read-only on corpus."""
import json, uuid, requests
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QURL = "http://localhost:6333"; COLL = "specloop_modules"
_NS = uuid.NAMESPACE_DNS

# Per-module SOUND results (gates passed for all; lists are genuinely-wrong = real CEX
# under the standard AXIS slave environment). ex=exercised&k-proven, dorm=guard-dormant,
# ni=non-inductive(holds in BMC), control=control-input-stability (needs control assumes).
PROVEN = {
 "axis_register":          dict(total=25, ex=11, dorm=13, ni=0, wrong=["ap_skid_buffer_backpressure_condition"]),
 "axis_pipeline_register": dict(total=16, ex=10, dorm=6,  ni=0, wrong=[]),
 "axis_srl_register":      dict(total=13, ex=13, dorm=0,  ni=0, wrong=[]),
 "axis_fifo":              dict(total=25, ex=15, dorm=8,  ni=1, wrong=["ap_rd_ptr_never_exceeds_commit"], cfg="DEPTH=256"),
 "axis_srl_fifo":          dict(total=19, ex=5,  dorm=1,  ni=8, wrong=["ap_master_valid_when_not_empty","ap_no_ready_when_full","ap_no_valid_when_empty","ap_no_write_when_full","ap_no_read_when_empty"]),
 "axis_adapter":           dict(total=11, ex=10, dorm=0,  ni=0, wrong=["ap_reset_slave_ready"]),
 "axis_frame_length_adjust":dict(total=19,ex=14, dorm=0,  ni=0, wrong=["ap_passthrough_length","ap_short_counter_bound","ap_long_counter_bound","ap_truncate_no_output","ap_output_pipeline_temp"]),
 "axis_frame_join":        dict(total=19, ex=17, dorm=0,  ni=1, wrong=["ap_tlast_only_on_final_port"]),
 "axis_cobs_encode":       dict(total=20, ex=17, dorm=0,  ni=0, wrong=["ap_input_count_resets_on_boundary","ap_output_count_decrements","ap_output_alternates_code_data"]),
 "axis_cobs_decode":       dict(total=26, ex=22, dorm=0,  ni=1, wrong=["ap_reset_s_ready","ap_no_simultaneous_temp_valids","ap_tlast_implies_idle"]),
 "axis_rate_limit":        dict(total=35, ex=23, dorm=0,  ni=6, wrong=["ap_master_valid_stable","ap_accumulator_bounded","ap_no_ready_when_paused","ap_skid_buffer_no_overflow","ap_rate_by_frame_pause_in_frame","ap_ready_implies_not_paused"]),
 "axis_mux":               dict(total=15, ex=12, dorm=0,  ni=0, wrong=["ap_frame_start","ap_no_ready_disabled","ap_valid_needs_frame"]),
 "axis_demux":             dict(total=24, ex=16, dorm=0,  ni=1, control=["ap_select_stable_after_start","ap_select_stable_during_frame","ap_drop_stable_after_start","ap_drop_stable_during_frame"], wrong=["ap_output_temp_mutex","ap_tlast_propagates","ap_ready_reflects_master"]),
}
FLAGGED_AXIS = {
 "axis_async_fifo":  "flagged_dual_clock — two clock domains (s_axis_aclk/m_axis_aclk); single-clock gated harness does not apply (gates ERROR). Needs a CDC environment.",
 "axis_ll_bridge":   "flagged_malformed_spec — assertion ap_axis_ready_inverse uses $past in a non-clocked block (illegal); spec cannot elaborate.",
}

DISCARD_GROUPS = {
 "Inverted skid / mutex semantics (full skid can have ready downstream; both skid/temp regs CAN be valid)":
   ["axis_register:ap_skid_buffer_backpressure_condition","axis_rate_limit:ap_skid_buffer_no_overflow","axis_cobs_decode:ap_no_simultaneous_temp_valids"],
 "Reset-polarity / combinational-reset flips (assert registered-reset behavior on inverted or combinational signals)":
   ["axis_cobs_decode:ap_reset_s_ready (asserts TREADY HIGH in reset; module resets it LOW)","axis_adapter:ap_reset_slave_ready (combinational bypass ready not reset-gated)"],
 "Config-dependent claims stated unconditionally (hold only for some rate_num/rate_denom/by_frame configs)":
   ["axis_rate_limit:ap_accumulator_bounded","axis_rate_limit:ap_no_ready_when_paused","axis_rate_limit:ap_ready_implies_not_paused","axis_rate_limit:ap_rate_by_frame_pause_in_frame","axis_rate_limit:ap_master_valid_stable"],
 "Over-strict FSM / counter / pointer transitions (the module's real legal paths violate the claim)":
   ["axis_fifo:ap_rd_ptr_never_exceeds_commit","axis_cobs_encode:ap_input_count_resets_on_boundary","axis_cobs_encode:ap_output_count_decrements","axis_cobs_encode:ap_output_alternates_code_data","axis_cobs_decode:ap_tlast_implies_idle","axis_mux:ap_frame_start","axis_mux:ap_no_ready_disabled","axis_mux:ap_valid_needs_frame","axis_frame_join:ap_tlast_only_on_final_port","axis_frame_length_adjust:ap_passthrough_length","axis_frame_length_adjust:ap_short_counter_bound","axis_frame_length_adjust:ap_long_counter_bound","axis_frame_length_adjust:ap_truncate_no_output","axis_frame_length_adjust:ap_output_pipeline_temp","axis_demux:ap_output_temp_mutex","axis_demux:ap_tlast_propagates","axis_demux:ap_ready_reflects_master"],
 "Mis-specified FIFO full/empty invariants (ignore simultaneous read/write timing)":
   ["axis_srl_fifo:ap_master_valid_when_not_empty","axis_srl_fifo:ap_no_ready_when_full","axis_srl_fifo:ap_no_valid_when_empty","axis_srl_fifo:ap_no_write_when_full","axis_srl_fifo:ap_no_read_when_empty"],
 "Control-input stability stated as ASSERTION (mis-categorized; would hold given a control-input assumption — NOT wrong content)":
   ["axis_demux:ap_select_stable_after_start","axis_demux:ap_select_stable_during_frame","axis_demux:ap_drop_stable_after_start","axis_demux:ap_drop_stable_during_frame"],
}

def qpatch(name, payload):
    pid = str(uuid.uuid5(_NS, f"specloop.module.{name}"))
    requests.put(f"{QURL}/collections/{COLL}/points/payload",
                 json={"payload": payload, "points": [pid]}, timeout=10)

# ---- write sound records + correct Qdrant ----
records = {}
for m, r in PROVEN.items():
    hold = r["ex"] + r["dorm"] + r["ni"]
    ctrl = len(r.get("control", []))
    wrong = len(r["wrong"])
    sound_conf = round(hold / r["total"], 3)
    records[m] = dict(status="soundly_reproven", config=r.get("cfg", "default"),
                      total=r["total"], exercised_proven=r["ex"], guard_dormant=r["dorm"],
                      non_inductive_holds=r["ni"], genuine_hold=hold,
                      needs_control_env=r.get("control", []), discarded_wrong=r["wrong"],
                      sound_confidence=sound_conf)
    qpatch(m, dict(sound_status="soundly_reproven", sound_proven=hold, sound_total=r["total"],
                   sound_discarded=wrong, confidence=sound_conf))
for m, why in FLAGGED_AXIS.items():
    records[m] = dict(status="flagged", reason=why)
    qpatch(m, dict(sound_status=why.split(" — ")[0]))

# flag the 31 non-AXIS modules (not re-proven; need their own protocol environment)
r = requests.post(f"{QURL}/collections/{COLL}/points/scroll", json={"limit":100,"with_payload":True,"with_vector":False})
import re
for p in r.json()["result"]["points"]:
    n = p["payload"].get("module_name")
    if not n: continue
    if n in PROVEN or n in FLAGGED_AXIS or p["payload"].get("assertion_count",0)==0:
        continue
    records[n] = dict(status="flagged_not_reproven",
                      reason="non-AXIS / no s_axis slave — needs its own protocol environment (wishbone/AXI4/req-grant/monitor/etc.)")
    qpatch(n, dict(sound_status="flagged_non_axis_needs_protocol_env"))

(ROOT/"work/recheck_axis/sound_results.json").write_text(json.dumps(records, indent=2))

# ---- discard list ----
md = ["# Genuinely-WRONG assertions discarded in the gated re-proof",
      "(real counterexamples under a sound, gate-verified AXIS environment; the modules are correct)\n"]
for grp, items in DISCARD_GROUPS.items():
    md.append(f"## {grp}")
    for it in items: md.append(f"- {it}")
    md.append("")
(ROOT/"work/recheck_axis/DISCARD_LIST.md").write_text("\n".join(md))

# ---- library-wide tally ----
T = sum(r["total"] for r in PROVEN.values())
EX = sum(r["ex"] for r in PROVEN.values()); DO = sum(r["dorm"] for r in PROVEN.values())
NI = sum(r["ni"] for r in PROVEN.values()); CT = sum(len(r.get("control",[])) for r in PROVEN.values())
WR = sum(len(r["wrong"]) for r in PROVEN.values())
print(f"SOUND AXIS modules re-proven: {len(PROVEN)}  | flagged AXIS: {len(FLAGGED_AXIS)}  | flagged non-AXIS: {len(records)-len(PROVEN)-len(FLAGGED_AXIS)}")
print(f"Across the {len(PROVEN)} proven modules: {T} assertions")
print(f"  genuine HOLD = {EX+DO+NI}  (exercised&k-proven {EX} + guard-dormant {DO} + non-inductive-holds {NI})")
print(f"  needs-control-env (deferred) = {CT}")
print(f"  DISCARDED genuinely-wrong   = {WR}   ({100*WR//T}% of proven-module assertions)")
print(f"  sound fraction = {EX+DO+NI}/{T} = {(EX+DO+NI)/T:.0%}")
print("wrote sound_results.json + DISCARD_LIST.md; corrected Qdrant sound_status/confidence")
