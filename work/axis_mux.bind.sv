module axis_mux_spec (
  input logic clk,
  input logic rst,
  input logic [31:0] s_axis_tdata,
  input logic [3:0] s_axis_tkeep,
  input logic [3:0] s_axis_tvalid,
  input logic [3:0] s_axis_tready,
  input logic [3:0] s_axis_tlast,
  input logic [31:0] s_axis_tid,
  input logic [31:0] s_axis_tdest,
  input logic [3:0] s_axis_tuser,
  input logic [7:0] m_axis_tdata,
  input logic m_axis_tkeep,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic [7:0] m_axis_tid,
  input logic [7:0] m_axis_tdest,
  input logic m_axis_tuser,
  input logic enable,
  input logic [1:0] select
);

  // Internal signal access (assuming these are visible via bind)
  wire frame_reg = axis_mux.frame_reg;
  wire [1:0] select_reg = axis_mux.select_reg;
  wire [3:0] s_axis_tready_reg = axis_mux.s_axis_tready_reg;
  wire m_axis_tvalid_reg = axis_mux.m_axis_tvalid_reg;
  wire temp_m_axis_tvalid_reg = axis_mux.temp_m_axis_tvalid_reg;
  wire m_axis_tvalid_int = axis_mux.m_axis_tvalid_int;

  // Decode logic for selected stream signals
  wire [7:0] current_s_tdata = s_axis_tdata[select_reg*8 +: 8];
  wire current_s_tvalid = s_axis_tvalid[select_reg];
  wire current_s_tready = s_axis_tready[select_reg];
  wire current_s_tlast = s_axis_tlast[select_reg];

  // One-hot check helper
  wire [3:0] s_axis_tready_onehot_check = s_axis_tready_reg & (s_axis_tready_reg - 4'b0001);

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_frame: assert(frame_reg == 0);
      ap_reset_select: assert(select_reg == 0);
      ap_reset_ready: assert(s_axis_tready_reg == 4'b0000);
      ap_reset_valid: assert(m_axis_tvalid_reg == 0);
      ap_reset_temp_valid: assert(temp_m_axis_tvalid_reg == 0);
    end

    if (!rst && $past(1'b1)) begin
      // Property 4: single_slave_ready_at_once
      ap_single_ready: assert(s_axis_tready_onehot_check == 4'b0000);

      // Property 5: select_stable_during_frame
      if ($past(frame_reg) && frame_reg) begin
        ap_select_stable: assert(select_reg == $past(select_reg));
      end

      // Property 6: frame_starts_on_valid_transfer
      if (!$past(frame_reg) && frame_reg) begin
        ap_frame_start: assert($past(enable) && $past(s_axis_tvalid[select]) && $past(s_axis_tready[select]));
      end

      // Property 7: frame_ends_on_tlast_transfer
      if ($past(frame_reg) && !frame_reg) begin
        ap_frame_end: assert($past(current_s_tvalid && current_s_tready && current_s_tlast));
      end

      // Property 8: no_ready_when_disabled
      if (!enable) begin
        ap_no_ready_disabled: assert(s_axis_tready_reg == 4'b0000);
      end

      // Property 9: ready_only_for_selected_stream
      if (s_axis_tready_reg != 4'b0000) begin
        ap_ready_selected: assert(s_axis_tready_reg == (4'b0001 << select_reg));
      end

      // Property 10: master_valid_requires_frame
      if (m_axis_tvalid) begin
        ap_valid_needs_frame: assert(frame_reg || $past(frame_reg));
      end

      // Property 12: select_range_valid
      ap_select_range: assert(select_reg <= 2'd3);

      // Property 15: no_output_valid_when_no_frame
      if (!frame_reg) begin
        ap_no_valid_no_frame: assert(!m_axis_tvalid_int);
      end

      // Property 16: backpressure_handling
      if ($past(m_axis_tvalid && !m_axis_tready && m_axis_tvalid_int)) begin
        ap_backpressure: assert(m_axis_tvalid || temp_m_axis_tvalid_reg);
      end
    end
  end

endmodule

bind axis_mux axis_mux_spec spec_inst(.*);