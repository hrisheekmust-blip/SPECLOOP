module axis_rate_limit_spec (
  input logic clk,
  input logic rst,
  input logic [7:0] s_axis_tdata,
  input logic s_axis_tkeep,
  input logic s_axis_tvalid,
  input logic s_axis_tready,
  input logic s_axis_tlast,
  input logic [7:0] s_axis_tid,
  input logic [7:0] s_axis_tdest,
  input logic s_axis_tuser,
  input logic [7:0] m_axis_tdata,
  input logic m_axis_tkeep,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic [7:0] m_axis_tid,
  input logic [7:0] m_axis_tdest,
  input logic m_axis_tuser,
  input logic [7:0] rate_num,
  input logic [7:0] rate_denom,
  input logic rate_by_frame
);

  wire [23:0] acc_reg;
  wire frame_reg;
  wire s_axis_tready_reg;
  wire m_axis_tvalid_reg;
  wire temp_m_axis_tvalid_reg;
  wire m_axis_tready_int_reg;
  wire [7:0] m_axis_tdata_reg;
  wire m_axis_tkeep_reg;
  wire m_axis_tlast_reg;
  wire [7:0] m_axis_tid_reg;
  wire [7:0] m_axis_tdest_reg;
  wire m_axis_tuser_reg;
  wire [7:0] temp_m_axis_tdata_reg;
  wire temp_m_axis_tkeep_reg;
  wire temp_m_axis_tlast_reg;
  wire [7:0] temp_m_axis_tid_reg;
  wire [7:0] temp_m_axis_tdest_reg;
  wire temp_m_axis_tuser_reg;

  assign acc_reg = axis_rate_limit.acc_reg;
  assign frame_reg = axis_rate_limit.frame_reg;
  assign s_axis_tready_reg = axis_rate_limit.s_axis_tready_reg;
  assign m_axis_tvalid_reg = axis_rate_limit.m_axis_tvalid_reg;
  assign temp_m_axis_tvalid_reg = axis_rate_limit.temp_m_axis_tvalid_reg;
  assign m_axis_tready_int_reg = axis_rate_limit.m_axis_tready_int_reg;
  assign m_axis_tdata_reg = axis_rate_limit.m_axis_tdata_reg;
  assign m_axis_tkeep_reg = axis_rate_limit.m_axis_tkeep_reg;
  assign m_axis_tlast_reg = axis_rate_limit.m_axis_tlast_reg;
  assign m_axis_tid_reg = axis_rate_limit.m_axis_tid_reg;
  assign m_axis_tdest_reg = axis_rate_limit.m_axis_tdest_reg;
  assign m_axis_tuser_reg = axis_rate_limit.m_axis_tuser_reg;
  assign temp_m_axis_tdata_reg = axis_rate_limit.temp_m_axis_tdata_reg;
  assign temp_m_axis_tkeep_reg = axis_rate_limit.temp_m_axis_tkeep_reg;
  assign temp_m_axis_tlast_reg = axis_rate_limit.temp_m_axis_tlast_reg;
  assign temp_m_axis_tid_reg = axis_rate_limit.temp_m_axis_tid_reg;
  assign temp_m_axis_tdest_reg = axis_rate_limit.temp_m_axis_tdest_reg;
  assign temp_m_axis_tuser_reg = axis_rate_limit.temp_m_axis_tuser_reg;

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_acc: assert(acc_reg == 24'd0);
      ap_reset_frame: assert(frame_reg == 1'b0);
      ap_reset_s_ready: assert(s_axis_tready_reg == 1'b0);
      ap_reset_m_valid: assert(m_axis_tvalid_reg == 1'b0);
      ap_reset_temp_valid: assert(temp_m_axis_tvalid_reg == 1'b0);
      ap_reset_m_ready_int: assert(m_axis_tready_int_reg == 1'b0);
    end
  end

  always @(posedge clk) begin
    if (!rst && m_axis_tvalid && !m_axis_tready && $past(!rst)) begin
      ap_master_valid_stable: assert(m_axis_tvalid == $past(m_axis_tvalid));
    end
  end

  always @(posedge clk) begin
    if (!rst && m_axis_tvalid && !m_axis_tready && $past(!rst && m_axis_tvalid && !m_axis_tready)) begin
      ap_master_data_stable_tdata: assert(m_axis_tdata == $past(m_axis_tdata));
      ap_master_data_stable_tkeep: assert(m_axis_tkeep == $past(m_axis_tkeep));
      ap_master_data_stable_tlast: assert(m_axis_tlast == $past(m_axis_tlast));
      ap_master_data_stable_tid: assert(m_axis_tid == $past(m_axis_tid));
      ap_master_data_stable_tdest: assert(m_axis_tdest == $past(m_axis_tdest));
      ap_master_data_stable_tuser: assert(m_axis_tuser == $past(m_axis_tuser));
    end
  end

  always @(posedge clk) begin
    if (!rst && rate_num > 0) begin
      ap_accumulator_bounded: assert(acc_reg < 2 * {16'd0, rate_num});
    end
  end

  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid && s_axis_tready && acc_reg < rate_num)) begin
      ap_accumulator_increment: assert(acc_reg == ($past(acc_reg) + $past(rate_denom) - $past(rate_num)));
    end
  end

  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(acc_reg >= rate_num && !(s_axis_tvalid && s_axis_tready))) begin
      ap_accumulator_decrement: assert(acc_reg == ($past(acc_reg) - $past(rate_num)));
    end
  end

  always @(posedge clk) begin
    if (!rst && acc_reg >= rate_num && (!rate_by_frame || !frame_reg)) begin
      ap_no_ready_when_paused: assert(!s_axis_tready);
    end
  end

  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid && s_axis_tready)) begin
      ap_frame_tracking: assert(frame_reg == !$past(s_axis_tlast));
    end
  end

  always @(posedge clk) begin
    if (!rst && !m_axis_tready) begin
      ap_skid_buffer_no_overflow: assert(!(temp_m_axis_tvalid_reg && m_axis_tvalid_reg));
    end
  end

  always @(posedge clk) begin
    if (!rst && rate_by_frame && frame_reg && acc_reg >= rate_num) begin
      ap_rate_by_frame_pause_in_frame: assert(s_axis_tready);
    end
  end

  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid && s_axis_tready && s_axis_tlast)) begin
      ap_frame_clear_on_tlast: assert(frame_reg == 1'b0);
    end
  end

  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid && s_axis_tready && !s_axis_tlast)) begin
      ap_frame_set_on_transfer: assert(frame_reg == 1'b1);
    end
  end

  always @(posedge clk) begin
    if (!rst && s_axis_tready) begin
      ap_ready_implies_not_paused: assert(acc_reg < rate_num || (rate_by_frame && frame_reg));
    end
  end

  always @(posedge clk) begin
    if (!rst && m_axis_tvalid_reg && $past(!rst && m_axis_tvalid_reg && !m_axis_tready)) begin
      ap_output_reg_stable_tdata: assert(m_axis_tdata_reg == $past(m_axis_tdata_reg));
      ap_output_reg_stable_tkeep: assert(m_axis_tkeep_reg == $past(m_axis_tkeep_reg));
      ap_output_reg_stable_tlast: assert(m_axis_tlast_reg == $past(m_axis_tlast_reg));
      ap_output_reg_stable_tid: assert(m_axis_tid_reg == $past(m_axis_tid_reg));
      ap_output_reg_stable_tdest: assert(m_axis_tdest_reg == $past(m_axis_tdest_reg));
      ap_output_reg_stable_tuser: assert(m_axis_tuser_reg == $past(m_axis_tuser_reg));
    end
  end

  always @(posedge clk) begin
    if (!rst && temp_m_axis_tvalid_reg && $past(!rst && temp_m_axis_tvalid_reg && m_axis_tvalid_reg)) begin
      ap_temp_reg_stable_tdata: assert(temp_m_axis_tdata_reg == $past(temp_m_axis_tdata_reg));
      ap_temp_reg_stable_tkeep: assert(temp_m_axis_tkeep_reg == $past(temp_m_axis_tkeep_reg));
      ap_temp_reg_stable_tlast: assert(temp_m_axis_tlast_reg == $past(temp_m_axis_tlast_reg));
      ap_temp_reg_stable_tid: assert(temp_m_axis_tid_reg == $past(temp_m_axis_tid_reg));
      ap_temp_reg_stable_tdest: assert(temp_m_axis_tdest_reg == $past(temp_m_axis_tdest_reg));
      ap_temp_reg_stable_tuser: assert(temp_m_axis_tuser_reg == $past(temp_m_axis_tuser_reg));
    end
  end

endmodule

bind axis_rate_limit axis_rate_limit_spec spec_inst(.*);