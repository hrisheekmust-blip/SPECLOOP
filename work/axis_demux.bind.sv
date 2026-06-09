module axis_demux_spec (
  input logic clk,
  input logic rst,
  input logic [7:0] s_axis_tdata,
  input logic s_axis_tkeep,
  input logic s_axis_tvalid,
  input logic s_axis_tready,
  input logic s_axis_tlast,
  input logic [7:0] s_axis_tid,
  input logic [9:0] s_axis_tdest,
  input logic s_axis_tuser,
  input logic [31:0] m_axis_tdata,
  input logic [3:0] m_axis_tkeep,
  input logic [3:0] m_axis_tvalid,
  input logic [3:0] m_axis_tready,
  input logic [3:0] m_axis_tlast,
  input logic [31:0] m_axis_tid,
  input logic [31:0] m_axis_tdest,
  input logic [3:0] m_axis_tuser,
  input logic enable,
  input logic drop,
  input logic [1:0] select
);

  // Internal state signals from DUT
  wire [1:0] select_reg = axis_demux.select_reg;
  wire drop_reg = axis_demux.drop_reg;
  wire frame_reg = axis_demux.frame_reg;
  wire s_axis_tready_reg = axis_demux.s_axis_tready_reg;
  wire [3:0] m_axis_tvalid_reg = axis_demux.m_axis_tvalid_reg;
  wire [3:0] temp_m_axis_tvalid_reg = axis_demux.temp_m_axis_tvalid_reg;
  wire [3:0] m_axis_tvalid_int = axis_demux.m_axis_tvalid_int;
  wire drop_ctl = axis_demux.drop_ctl;
  wire [1:0] select_ctl = axis_demux.select_ctl;
  wire m_axis_tready_int_early = axis_demux.m_axis_tready_int_early;
  wire store_axis_int_to_output = axis_demux.store_axis_int_to_output;
  wire store_axis_int_to_temp = axis_demux.store_axis_int_to_temp;
  wire store_axis_temp_to_output = axis_demux.store_axis_temp_to_output;

  // Helper signals
  wire slave_transfer = s_axis_tvalid && s_axis_tready;
  wire frame_start = slave_transfer && !frame_reg;
  wire frame_end = slave_transfer && s_axis_tlast;

  // Popcount for one-hot checking
  wire [2:0] m_axis_tvalid_reg_popcount = m_axis_tvalid_reg[0] + m_axis_tvalid_reg[1] + m_axis_tvalid_reg[2] + m_axis_tvalid_reg[3];
  wire [2:0] temp_m_axis_tvalid_reg_popcount = temp_m_axis_tvalid_reg[0] + temp_m_axis_tvalid_reg[1] + temp_m_axis_tvalid_reg[2] + temp_m_axis_tvalid_reg[3];
  wire [1:0] skid_control_popcount = store_axis_int_to_output + store_axis_int_to_temp + store_axis_temp_to_output;

  always @(posedge clk) begin
    // Property 1: reset_clears_all_state
    if (rst) begin
      ap_reset_frame: assert(!frame_reg);
      ap_reset_drop: assert(!drop_reg);
      ap_reset_select: assert(select_reg == 0);
      ap_reset_s_tready: assert(!s_axis_tready_reg);
      ap_reset_m_tvalid: assert(m_axis_tvalid_reg == 0);
      ap_reset_temp_m_tvalid: assert(temp_m_axis_tvalid_reg == 0);
    end

    // Property 3: master_tvalid_one_hot_or_zero
    if (!rst) begin
      ap_m_tvalid_onehot0: assert(m_axis_tvalid_reg_popcount <= 1);
    end

    // Property 4: frame_atomicity_select_stable
    if (!rst && $past(!rst) && $past(frame_start)) begin
      ap_select_stable_after_start: assert(select_reg == $past(select_ctl));
    end
    if (!rst && $past(!rst) && frame_reg && !frame_end) begin
      ap_select_stable_during_frame: assert($stable(select_reg));
    end

    // Property 5: frame_atomicity_drop_stable
    if (!rst && $past(!rst) && $past(frame_start)) begin
      ap_drop_stable_after_start: assert(drop_reg == $past(drop_ctl));
    end
    if (!rst && $past(!rst) && frame_reg && !frame_end) begin
      ap_drop_stable_during_frame: assert($stable(drop_reg));
    end

    // Property 6: drop_mode_suppresses_master_valid
    if (!rst && drop_ctl) begin
      ap_drop_suppresses_valid: assert(m_axis_tvalid_int == 0);
    end

    // Property 7: frame_tracking_state_machine
    if (!rst && $past(!rst) && $past(!frame_reg && slave_transfer && !s_axis_tlast)) begin
      ap_frame_set_on_start: assert(frame_reg);
    end
    if (!rst && $past(!rst) && $past(frame_reg && slave_transfer && s_axis_tlast)) begin
      ap_frame_clear_on_end: assert(!frame_reg);
    end

    // Property 9: ready_always_high_when_dropping
    if (!rst && drop_ctl) begin
      ap_ready_high_when_drop: assert(s_axis_tready_reg || !enable);
    end

    // Property 10: select_within_valid_range
    if (!rst && !drop_reg && frame_reg) begin
      ap_select_in_range: assert(select_reg < 4);
    end

    // Property 11: no_simultaneous_output_and_temp_valid
    if (!rst) begin
      ap_output_temp_mutex: assert((m_axis_tvalid_reg & temp_m_axis_tvalid_reg) == 0);
    end

    // Property 12: enable_gates_slave_ready
    if (!rst && !enable) begin
      ap_enable_gates_ready: assert(!s_axis_tready);
    end

    // Property 13: data_routing_to_selected_master
    if (!rst && slave_transfer && !drop_ctl) begin
      ap_routing_onehot: assert(m_axis_tvalid_int == (4'b0001 << select_ctl));
    end

    // Property 14: tlast_propagation_to_selected_master
    if (!rst && slave_transfer && s_axis_tlast && !drop_ctl) begin
      ap_tlast_propagates: assert(m_axis_tlast[select_ctl]);
    end

    // Property 15: skid_buffer_mutual_exclusion
    if (!rst) begin
      ap_skid_mutex: assert(skid_control_popcount <= 1);
    end

    // Additional safety: temp valid is one-hot-or-zero
    if (!rst) begin
      ap_temp_tvalid_onehot0: assert(temp_m_axis_tvalid_reg_popcount <= 1);
    end

    // Frame tracking consistency
    if (!rst && !frame_reg) begin
      ap_no_frame_implies_no_drop: assert(!drop_reg || $past(frame_end));
    end

    // Ready propagation when enabled and not dropping
    if (!rst && enable && !drop_ctl) begin
      ap_ready_reflects_master: assert(s_axis_tready == (m_axis_tready_int_early && enable));
    end
  end

endmodule

bind axis_demux axis_demux_spec spec_inst(.*);