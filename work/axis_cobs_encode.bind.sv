module axis_cobs_encode_spec (
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
  input logic m_axis_tuser,
  input logic [1:0] input_state_reg,
  input logic [0:0] output_state_reg,
  input logic [7:0] input_count_reg,
  input logic [7:0] output_count_reg,
  input logic fail_frame_reg,
  input logic m_axis_tvalid_reg,
  input logic temp_m_axis_tvalid_reg,
  input logic m_axis_tready_int_reg,
  input logic s_axis_tready_mask,
  input logic code_fifo_in_tready,
  input logic data_fifo_in_tready,
  input logic code_fifo_in_tvalid,
  input logic [7:0] code_fifo_in_tdata,
  input logic code_fifo_in_tuser,
  input logic data_fifo_in_tvalid,
  input logic [7:0] data_fifo_in_tdata,
  input logic code_fifo_out_tvalid,
  input logic [7:0] code_fifo_out_tdata,
  input logic code_fifo_out_tuser,
  input logic code_fifo_out_tready,
  input logic data_fifo_out_tvalid,
  input logic [7:0] data_fifo_out_tdata,
  input logic data_fifo_out_tready
);

  localparam [1:0] INPUT_STATE_IDLE = 2'd0;
  localparam [1:0] INPUT_STATE_SEGMENT = 2'd1;
  localparam [1:0] INPUT_STATE_FINAL_ZERO = 2'd2;
  localparam [1:0] INPUT_STATE_APPEND_ZERO = 2'd3;
  localparam [0:0] OUTPUT_STATE_IDLE = 1'd0;
  localparam [0:0] OUTPUT_STATE_SEGMENT = 1'd1;

  wire s_axis_transfer;
  wire m_axis_transfer;
  assign s_axis_transfer = s_axis_tvalid && s_axis_tready;
  assign m_axis_transfer = m_axis_tvalid && m_axis_tready;

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_input_state: assert(input_state_reg == INPUT_STATE_IDLE);
      ap_reset_output_state: assert(output_state_reg == OUTPUT_STATE_IDLE);
      ap_reset_m_axis_tvalid: assert(!m_axis_tvalid_reg);
      ap_reset_temp_m_axis_tvalid: assert(!temp_m_axis_tvalid_reg);
    end
  end

  always @(posedge clk) begin
    if ($past(1'b1) && $past(rst)) begin
      ap_reset_to_input_idle: assert(input_state_reg == INPUT_STATE_IDLE);
      ap_reset_to_output_idle: assert(output_state_reg == OUTPUT_STATE_IDLE);
    end
  end

  always @(posedge clk) begin
    if (!rst && s_axis_tvalid && !s_axis_tready) begin
      if ($past(!rst) && $past(s_axis_tvalid && !s_axis_tready)) begin
        ap_slave_data_stable: assert($stable(s_axis_tdata));
      end
    end
  end

  always @(posedge clk) begin
    if (!rst && m_axis_tvalid && !m_axis_tready) begin
      if ($past(!rst) && $past(m_axis_tvalid && !m_axis_tready)) begin
        ap_master_data_stable: assert($stable(m_axis_tdata));
        ap_master_tlast_stable: assert($stable(m_axis_tlast));
        ap_master_tuser_stable: assert($stable(m_axis_tuser));
      end
    end
  end

  always @(posedge clk) begin
    if (!rst && s_axis_tready) begin
      ap_slave_ready_depends_on_fifos: assert(code_fifo_in_tready && data_fifo_in_tready && s_axis_tready_mask);
    end
  end

  always @(posedge clk) begin
    if (!rst) begin
      ap_input_count_max: assert(input_count_reg <= 8'd254);
    end
  end

  always @(posedge clk) begin
    if ($past(!rst) && $past(input_state_reg == INPUT_STATE_SEGMENT && s_axis_tvalid && s_axis_tready && input_count_reg == 8'd254 && s_axis_tdata != 8'd0)) begin
      ap_input_count_resets_on_boundary: assert(input_count_reg == 8'd1);
    end
  end

  always @(posedge clk) begin
    if ($past(!rst) && $past(output_state_reg == OUTPUT_STATE_SEGMENT && m_axis_tready_int_reg && data_fifo_out_tvalid && output_count_reg > 8'd1)) begin
      ap_output_count_decrements: assert(output_count_reg == $past(output_count_reg) - 8'd1);
    end
  end

  always @(posedge clk) begin
    if ($past(!rst) && $past(input_state_reg != INPUT_STATE_IDLE) && input_state_reg == INPUT_STATE_IDLE) begin
      ap_fail_frame_clears_on_idle: assert(!fail_frame_reg);
    end
  end

  always @(posedge clk) begin
    if (!rst) begin
      ap_fsm_input_valid_states: assert((input_state_reg == INPUT_STATE_IDLE) || (input_state_reg == INPUT_STATE_SEGMENT) || (input_state_reg == INPUT_STATE_FINAL_ZERO) || (input_state_reg == INPUT_STATE_APPEND_ZERO));
    end
  end

  always @(posedge clk) begin
    if (!rst) begin
      ap_fsm_output_valid_states: assert((output_state_reg == OUTPUT_STATE_IDLE) || (output_state_reg == OUTPUT_STATE_SEGMENT));
    end
  end

  always @(posedge clk) begin
    if (!rst && input_state_reg == INPUT_STATE_SEGMENT && s_axis_tvalid && s_axis_tready) begin
      if (s_axis_tdata == 8'd0 || input_count_reg == 8'd254) begin
        ap_code_fifo_write_on_zero_or_boundary: assert(code_fifo_in_tvalid);
      end
    end
  end

  always @(posedge clk) begin
    if (!rst && data_fifo_in_tvalid && ((input_state_reg == INPUT_STATE_IDLE) || (input_state_reg == INPUT_STATE_SEGMENT)) && !s_axis_tuser) begin
      ap_data_fifo_nonzero_bytes: assert(data_fifo_in_tdata != 8'd0);
    end
  end

  always @(posedge clk) begin
    if ($past(!rst) && $past(output_state_reg == OUTPUT_STATE_IDLE && code_fifo_out_tready && code_fifo_out_tvalid && code_fifo_out_tdata > 8'd1 && !code_fifo_out_tuser)) begin
      ap_output_alternates_code_data: assert(output_state_reg == OUTPUT_STATE_SEGMENT);
    end
  end

endmodule

bind axis_cobs_encode axis_cobs_encode_spec spec_inst(.*);