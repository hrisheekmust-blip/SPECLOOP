module axis_adapter_spec (
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

  // Decode bypass mode condition at module level
  wire bypass_mode;
  assign bypass_mode = 1'b1; // S_BYTE_LANES == M_BYTE_LANES for default parameters

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_master_valid: assert(!m_axis_tvalid);
      ap_reset_slave_ready: assert(!s_axis_tready);
    end

    if (!rst && $past(!rst)) begin
      // Slave interface stability during handshake
      if ($past(s_axis_tvalid && !s_axis_tready)) begin
        ap_slave_valid_stable: assert(s_axis_tvalid);
        ap_slave_data_stable: assert(s_axis_tdata == $past(s_axis_tdata));
        ap_slave_tlast_stable: assert(s_axis_tlast == $past(s_axis_tlast));
      end

      // Master interface stability during handshake
      if ($past(m_axis_tvalid && !m_axis_tready)) begin
        ap_master_valid_stable: assert(m_axis_tvalid);
        ap_master_data_stable: assert(m_axis_tdata == $past(m_axis_tdata));
      end

      // Bypass mode passthrough properties
      if (bypass_mode) begin
        ap_passthrough_ready: assert(s_axis_tready == m_axis_tready);
        ap_passthrough_valid: assert(m_axis_tvalid == s_axis_tvalid);
        
        if (s_axis_tvalid) begin
          ap_passthrough_data: assert(m_axis_tdata == s_axis_tdata);
          ap_passthrough_tlast: assert(m_axis_tlast == s_axis_tlast);
        end
      end
    end
  end

endmodule

bind axis_adapter axis_adapter_spec spec_inst(.*);