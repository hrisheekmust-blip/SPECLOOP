module axis_register_spec (
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
  input logic m_axis_tuser
);

  parameter DATA_WIDTH = 8;
  parameter KEEP_ENABLE = 1'b0;
  parameter KEEP_WIDTH = 1;
  parameter LAST_ENABLE = 1;
  parameter ID_ENABLE = 0;
  parameter ID_WIDTH = 8;
  parameter DEST_ENABLE = 0;
  parameter DEST_WIDTH = 8;
  parameter USER_ENABLE = 1;
  parameter USER_WIDTH = 1;
  parameter REG_TYPE = 2;

  // Reset assertions
  always @(posedge clk) begin
    if (rst) begin
      ap_reset_clears_master_valid: assert(m_axis_tvalid == 1'b0);
      ap_reset_clears_slave_ready: assert(s_axis_tready == 1'b0);
    end
  end

  // AXI-Stream handshake protocol - slave side
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid) && !$past(s_axis_tready)) begin
      ap_slave_valid_stable_until_ready: assert(s_axis_tvalid);
    end
  end

  // AXI-Stream handshake protocol - master side
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_valid_stable_until_ready: assert(m_axis_tvalid);
    end
  end

  // Master data stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_data_stable_until_ready: assert(m_axis_tdata == $past(m_axis_tdata));
    end
  end

  // Master tlast stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && LAST_ENABLE && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_tlast_stable_until_ready: assert(m_axis_tlast == $past(m_axis_tlast));
    end
  end

  // Master tkeep stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && KEEP_ENABLE && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_tkeep_stable_until_ready: assert(m_axis_tkeep == $past(m_axis_tkeep));
    end
  end

  // Master tid stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && ID_ENABLE && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_tid_stable_until_ready: assert(m_axis_tid == $past(m_axis_tid));
    end
  end

  // Master tdest stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && DEST_ENABLE && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_tdest_stable_until_ready: assert(m_axis_tdest == $past(m_axis_tdest));
    end
  end

  // Master tuser stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && USER_ENABLE && $past(m_axis_tvalid) && !$past(m_axis_tready)) begin
      ap_master_tuser_stable_until_ready: assert(m_axis_tuser == $past(m_axis_tuser));
    end
  end

  // Data transfer on slave handshake (REG_TYPE > 0)
  always @(posedge clk) begin
    if (!rst && $past(!rst) && REG_TYPE > 0 && $past(s_axis_tvalid && s_axis_tready)) begin
      ap_data_transfer_produces_master_valid: assert(m_axis_tvalid || $past(m_axis_tvalid));
    end
  end

  // Skid buffer throughput property
  always @(posedge clk) begin
    if (!rst && REG_TYPE == 2 && !s_axis_tready) begin
      ap_skid_buffer_backpressure_condition: assert(m_axis_tvalid && !m_axis_tready);
    end
  end

  // Disabled tkeep constant high
  always @(posedge clk) begin
    if (!rst && !KEEP_ENABLE) begin
      ap_disabled_tkeep_constant_high: assert(m_axis_tkeep == {KEEP_WIDTH{1'b1}});
    end
  end

  // Disabled tlast constant high
  always @(posedge clk) begin
    if (!rst && !LAST_ENABLE) begin
      ap_disabled_tlast_constant_high: assert(m_axis_tlast == 1'b1);
    end
  end

  // Disabled tid constant zero
  always @(posedge clk) begin
    if (!rst && !ID_ENABLE) begin
      ap_disabled_tid_constant_zero: assert(m_axis_tid == {ID_WIDTH{1'b0}});
    end
  end

  // Disabled tdest constant zero
  always @(posedge clk) begin
    if (!rst && !DEST_ENABLE) begin
      ap_disabled_tdest_constant_zero: assert(m_axis_tdest == {DEST_WIDTH{1'b0}});
    end
  end

  // Disabled tuser constant zero
  always @(posedge clk) begin
    if (!rst && !USER_ENABLE) begin
      ap_disabled_tuser_constant_zero: assert(m_axis_tuser == {USER_WIDTH{1'b0}});
    end
  end

  // REG_TYPE=0 bypass mode - combinational passthrough
  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0) begin
      ap_bypass_data_passthrough: assert(m_axis_tdata == s_axis_tdata);
      ap_bypass_valid_passthrough: assert(m_axis_tvalid == s_axis_tvalid);
      ap_bypass_ready_passthrough: assert(s_axis_tready == m_axis_tready);
    end
  end

  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0 && LAST_ENABLE) begin
      ap_bypass_tlast_passthrough: assert(m_axis_tlast == s_axis_tlast);
    end
  end

  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0 && KEEP_ENABLE) begin
      ap_bypass_tkeep_passthrough: assert(m_axis_tkeep == s_axis_tkeep);
    end
  end

  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0 && ID_ENABLE) begin
      ap_bypass_tid_passthrough: assert(m_axis_tid == s_axis_tid);
    end
  end

  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0 && DEST_ENABLE) begin
      ap_bypass_tdest_passthrough: assert(m_axis_tdest == s_axis_tdest);
    end
  end

  always @(posedge clk) begin
    if (!rst && REG_TYPE == 0 && USER_ENABLE) begin
      ap_bypass_tuser_passthrough: assert(m_axis_tuser == s_axis_tuser);
    end
  end

endmodule

bind axis_register axis_register_spec spec_inst(.*);