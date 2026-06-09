module axis_srl_register_spec (
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

  // Internal state signals from DUT (accessed via hierarchical reference)
  wire ptr_reg = axis_srl_register.ptr_reg;
  wire full_reg = axis_srl_register.full_reg;
  wire valid_reg_0 = axis_srl_register.valid_reg[0];
  wire valid_reg_1 = axis_srl_register.valid_reg[1];

  // Reset assertions
  always @(posedge clk) begin
    if (rst) begin
      ap_reset_ptr: assert(ptr_reg == 0);
      ap_reset_full: assert(full_reg == 0);
      ap_reset_valid0: assert(valid_reg_0 == 0);
      ap_reset_valid1: assert(valid_reg_1 == 0);
    end
  end

  // Interface protocol assertions
  always @(posedge clk) begin
    if (!rst) begin
      ap_ready_inverse_full: assert(s_axis_tready == !full_reg);
      ap_master_valid_from_reg: assert(m_axis_tvalid == (ptr_reg ? valid_reg_1 : valid_reg_0));
    end
  end

  // Functional invariants
  always @(posedge clk) begin
    if (!rst) begin
      ap_ptr_binary: assert(ptr_reg == 0 || ptr_reg == 1);
    end
  end

  // Temporal behavior with $past guards
  always @(posedge clk) begin
    if ($past(1'b1) && !rst && !$past(rst)) begin
      // Full register updates based on backpressure
      if ($past(m_axis_tvalid && !m_axis_tready)) begin
        ap_full_on_backpressure: assert(full_reg == 1);
      end

      // Pointer resets when master is ready
      if ($past(m_axis_tready)) begin
        ap_ptr_reset_on_ready: assert(ptr_reg == 0);
      end

      // Data stability when output valid but not ready
      if ($past(m_axis_tvalid && !m_axis_tready && !s_axis_tready)) begin
        ap_data_stable: assert(m_axis_tdata == $past(m_axis_tdata));
      end

      // Valid propagation on slave handshake
      if ($past(s_axis_tvalid && s_axis_tready)) begin
        ap_valid_after_write: assert(valid_reg_0 == 1);
      end

      // Pointer update when writing without master ready
      if ($past(s_axis_tready && !m_axis_tready)) begin
        ap_ptr_update: assert(ptr_reg == $past(valid_reg_0));
      end
    end
  end

  // Safety property: valid entries require prior write or persistence
  always @(posedge clk) begin
    if ($past(1'b1) && !rst && !$past(rst)) begin
      if (valid_reg_0 && !$past(s_axis_tvalid && s_axis_tready)) begin
        ap_valid_persistence: assert($past(valid_reg_0));
      end
    end
  end

endmodule

bind axis_srl_register axis_srl_register_spec spec_inst(.*);