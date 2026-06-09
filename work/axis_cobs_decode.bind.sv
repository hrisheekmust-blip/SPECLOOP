module axis_cobs_decode_spec (
  input logic clk,
  input logic rst,
  input logic [7:0] s_axis_tdata,
  input logic s_axis_tvalid,
  input logic s_axis_tready,
  input logic s_axis_tlast,
  input logic s_axis_tuser,
  input logic [7:0] m_axis_tdata,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic m_axis_tuser
);

  // Internal state and register signals for observation
  wire [1:0] state_reg = axis_cobs_decode.state_reg;
  wire [7:0] count_reg = axis_cobs_decode.count_reg;
  wire suppress_zero_reg = axis_cobs_decode.suppress_zero_reg;
  wire [7:0] temp_tdata_reg = axis_cobs_decode.temp_tdata_reg;
  wire temp_tvalid_reg = axis_cobs_decode.temp_tvalid_reg;
  wire m_axis_tvalid_reg = axis_cobs_decode.m_axis_tvalid_reg;
  wire temp_m_axis_tvalid_reg = axis_cobs_decode.temp_m_axis_tvalid_reg;
  wire [7:0] temp_m_axis_tdata_reg = axis_cobs_decode.temp_m_axis_tdata_reg;
  wire m_axis_tlast_int = axis_cobs_decode.m_axis_tlast_int;
  wire m_axis_tuser_int = axis_cobs_decode.m_axis_tuser_int;
  wire s_axis_tready_reg = axis_cobs_decode.s_axis_tready_reg;
  wire [7:0] m_axis_tdata_reg = axis_cobs_decode.m_axis_tdata_reg;
  wire m_axis_tlast_reg = axis_cobs_decode.m_axis_tlast_reg;
  wire m_axis_tuser_reg = axis_cobs_decode.m_axis_tuser_reg;
  wire m_axis_tready_int_reg = axis_cobs_decode.m_axis_tready_int_reg;

  localparam STATE_IDLE = 2'd0;
  localparam STATE_SEGMENT = 2'd1;
  localparam STATE_NEXT_SEGMENT = 2'd2;

  // Handshake detection wires
  wire s_handshake;
  wire m_handshake;
  assign s_handshake = s_axis_tvalid && s_axis_tready;
  assign m_handshake = m_axis_tvalid && m_axis_tready;

  // Reset assertions
  always @(posedge clk) begin
    if (rst) begin
      ap_reset_state: assert(state_reg == STATE_IDLE);
      ap_reset_m_valid: assert(!m_axis_tvalid_reg);
      ap_reset_temp_m_valid: assert(!temp_m_axis_tvalid_reg);
      ap_reset_temp_valid: assert(!temp_tvalid_reg);
      ap_reset_s_ready: assert(s_axis_tready_reg);
    end
  end

  // AXI-Stream slave interface stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(s_axis_tvalid && !s_axis_tready)) begin
      ap_s_axis_tdata_stable: assert(s_axis_tdata == $past(s_axis_tdata));
      ap_s_axis_tlast_stable: assert(s_axis_tlast == $past(s_axis_tlast));
    end
  end

  // AXI-Stream master interface stability
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(m_axis_tvalid && !m_axis_tready)) begin
      ap_m_axis_tdata_stable: assert(m_axis_tdata == $past(m_axis_tdata));
      ap_m_axis_tlast_stable: assert(m_axis_tlast == $past(m_axis_tlast));
      ap_m_axis_tuser_stable: assert(m_axis_tuser == $past(m_axis_tuser));
    end
  end

  // Count register valid range
  always @(posedge clk) begin
    if (!rst) begin
      ap_count_valid_range: assert(count_reg <= 8'd254);
    end
  end

  // Count decrement in STATE_SEGMENT
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_SEGMENT && s_handshake && s_axis_tdata != 8'd0 && !s_axis_tlast)) begin
      ap_count_decrement: assert(count_reg == $past(count_reg) - 8'd1);
    end
  end

  // Suppress zero flag set on 255 count
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_IDLE && s_handshake && s_axis_tdata == 8'd255)) begin
      ap_suppress_zero_on_255: assert(suppress_zero_reg);
    end
  end

  // Zero insertion when not suppressed
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_NEXT_SEGMENT && s_handshake && !suppress_zero_reg && s_axis_tdata != 8'd0 && !s_axis_tlast)) begin
      ap_zero_insertion: assert(temp_tdata_reg == 8'd0 && temp_tvalid_reg);
    end
  end

  // Error on zero byte in STATE_SEGMENT
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_SEGMENT && s_handshake && s_axis_tdata == 8'd0)) begin
      ap_error_zero_in_segment_state: assert(state_reg == STATE_IDLE);
      ap_error_zero_in_segment_user: assert($past(m_axis_tuser_int));
      ap_error_zero_in_segment_last: assert($past(m_axis_tlast_int));
    end
  end

  // Error on premature tlast in STATE_SEGMENT
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_SEGMENT && s_handshake && s_axis_tlast && count_reg != 8'd1)) begin
      ap_error_premature_tlast_state: assert(state_reg == STATE_IDLE);
      ap_error_premature_tlast_user: assert($past(m_axis_tuser_int));
      ap_error_premature_tlast_last: assert($past(m_axis_tlast_int));
    end
  end

  // No simultaneous temp valids
  always @(posedge clk) begin
    if (!rst) begin
      ap_no_simultaneous_temp_valids: assert(!(temp_tvalid_reg && temp_m_axis_tvalid_reg));
    end
  end

  // State transition: IDLE to SEGMENT
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_IDLE && s_handshake && s_axis_tdata != 8'd0 && s_axis_tdata != 8'd1)) begin
      ap_idle_to_segment: assert(state_reg == STATE_SEGMENT);
    end
  end

  // State transition: SEGMENT to NEXT_SEGMENT
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(state_reg == STATE_SEGMENT && count_reg == 8'd1 && s_handshake && s_axis_tdata != 8'd0 && !s_axis_tlast)) begin
      ap_segment_to_next_segment: assert(state_reg == STATE_NEXT_SEGMENT);
    end
  end

  // tlast only when transitioning to IDLE
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(m_axis_tlast_int)) begin
      ap_tlast_implies_idle: assert(state_reg == STATE_IDLE);
    end
  end

  // Output valid persistence
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(m_axis_tvalid && !m_axis_tready)) begin
      ap_output_valid_persistence: assert(m_axis_tvalid);
    end
  end

  // Skid buffer data preservation
  always @(posedge clk) begin
    if (!rst && $past(!rst) && $past(temp_m_axis_tvalid_reg && m_axis_tready && !m_axis_tready_int_reg)) begin
      ap_skid_buffer_preservation: assert(m_axis_tdata_reg == $past(temp_m_axis_tdata_reg));
    end
  end

endmodule

bind axis_cobs_decode axis_cobs_decode_spec spec_inst(.*);