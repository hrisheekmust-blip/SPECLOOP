module axis_pipeline_register_spec #(
  parameter DATA_WIDTH = 8,
  parameter KEEP_ENABLE = 1'b0,
  parameter KEEP_WIDTH = 1,
  parameter LAST_ENABLE = 1,
  parameter ID_ENABLE = 0,
  parameter ID_WIDTH = 8,
  parameter DEST_ENABLE = 0,
  parameter DEST_WIDTH = 8,
  parameter USER_ENABLE = 1,
  parameter USER_WIDTH = 1,
  parameter REG_TYPE = 2,
  parameter LENGTH = 2
) (
  input logic clk,
  input logic rst,
  input logic [DATA_WIDTH-1:0] s_axis_tdata,
  input logic [KEEP_WIDTH-1:0] s_axis_tkeep,
  input logic s_axis_tvalid,
  input logic s_axis_tready,
  input logic s_axis_tlast,
  input logic [ID_WIDTH-1:0] s_axis_tid,
  input logic [DEST_WIDTH-1:0] s_axis_tdest,
  input logic [USER_WIDTH-1:0] s_axis_tuser,
  input logic [DATA_WIDTH-1:0] m_axis_tdata,
  input logic [KEEP_WIDTH-1:0] m_axis_tkeep,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic [ID_WIDTH-1:0] m_axis_tid,
  input logic [DEST_WIDTH-1:0] m_axis_tdest,
  input logic [USER_WIDTH-1:0] m_axis_tuser
);

  wire slave_handshake;
  wire master_handshake;
  assign slave_handshake = s_axis_tvalid && s_axis_tready;
  assign master_handshake = m_axis_tvalid && m_axis_tready;

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_master_valid: assert(!m_axis_tvalid);
      ap_reset_slave_ready: assert(!s_axis_tready);
    end

    if ($past(1'b1) && $past(rst)) begin
      ap_reset_clears_master_valid: assert(!m_axis_tvalid);
      ap_reset_clears_slave_ready: assert(!s_axis_tready);
    end

    if (!rst && $past(1'b1) && !$past(rst)) begin
      if ($past(s_axis_tvalid) && !$past(s_axis_tready)) begin
        ap_slave_data_stable_tdata: assert(s_axis_tdata == $past(s_axis_tdata));
        if (KEEP_ENABLE) begin
          ap_slave_data_stable_tkeep: assert(s_axis_tkeep == $past(s_axis_tkeep));
        end
        if (LAST_ENABLE) begin
          ap_slave_data_stable_tlast: assert(s_axis_tlast == $past(s_axis_tlast));
        end
        if (ID_ENABLE) begin
          ap_slave_data_stable_tid: assert(s_axis_tid == $past(s_axis_tid));
        end
        if (DEST_ENABLE) begin
          ap_slave_data_stable_tdest: assert(s_axis_tdest == $past(s_axis_tdest));
        end
        if (USER_ENABLE) begin
          ap_slave_data_stable_tuser: assert(s_axis_tuser == $past(s_axis_tuser));
        end
      end

      if ($past(m_axis_tvalid) && !$past(m_axis_tready)) begin
        ap_master_data_stable_tdata: assert(m_axis_tdata == $past(m_axis_tdata));
        if (KEEP_ENABLE) begin
          ap_master_data_stable_tkeep: assert(m_axis_tkeep == $past(m_axis_tkeep));
        end
        if (LAST_ENABLE) begin
          ap_master_data_stable_tlast: assert(m_axis_tlast == $past(m_axis_tlast));
        end
        if (ID_ENABLE) begin
          ap_master_data_stable_tid: assert(m_axis_tid == $past(m_axis_tid));
        end
        if (DEST_ENABLE) begin
          ap_master_data_stable_tdest: assert(m_axis_tdest == $past(m_axis_tdest));
        end
        if (USER_ENABLE) begin
          ap_master_data_stable_tuser: assert(m_axis_tuser == $past(m_axis_tuser));
        end
      end
    end
  end

endmodule

bind axis_pipeline_register axis_pipeline_register_spec #(
  .DATA_WIDTH(DATA_WIDTH),
  .KEEP_ENABLE(KEEP_ENABLE),
  .KEEP_WIDTH(KEEP_WIDTH),
  .LAST_ENABLE(LAST_ENABLE),
  .ID_ENABLE(ID_ENABLE),
  .ID_WIDTH(ID_WIDTH),
  .DEST_ENABLE(DEST_ENABLE),
  .DEST_WIDTH(DEST_WIDTH),
  .USER_ENABLE(USER_ENABLE),
  .USER_WIDTH(USER_WIDTH),
  .REG_TYPE(REG_TYPE),
  .LENGTH(LENGTH)
) spec_inst(.*);