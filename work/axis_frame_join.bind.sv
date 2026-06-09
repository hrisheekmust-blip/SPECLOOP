module axis_frame_join_spec #(
  parameter S_COUNT = 4,
  parameter DATA_WIDTH = 8,
  parameter TAG_ENABLE = 1,
  parameter TAG_WIDTH = 16,
  parameter CL_S_COUNT = 2,
  parameter TAG_WORD_WIDTH = 2,
  parameter CL_TAG_WORD_WIDTH = 1,
  parameter STATE_IDLE = 2'b0,
  parameter STATE_WRITE_TAG = 2'b1,
  parameter STATE_TRANSFER = 2'b10
) (
  input logic clk,
  input logic rst,
  input logic [S_COUNT*DATA_WIDTH-1:0] s_axis_tdata,
  input logic [S_COUNT-1:0] s_axis_tvalid,
  input logic [S_COUNT-1:0] s_axis_tready,
  input logic [S_COUNT-1:0] s_axis_tlast,
  input logic [S_COUNT-1:0] s_axis_tuser,
  input logic [DATA_WIDTH-1:0] m_axis_tdata,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic m_axis_tuser,
  input logic [TAG_WIDTH-1:0] tag,
  input logic busy
);

  wire [1:0] state_reg;
  wire [CL_TAG_WORD_WIDTH-1:0] frame_ptr_reg;
  wire [CL_S_COUNT-1:0] port_sel_reg;
  wire busy_reg;
  wire m_axis_tvalid_reg;
  wire m_axis_tready_int_reg;
  wire temp_m_axis_tvalid_reg;
  wire m_axis_tvalid_int;

  assign state_reg = axis_frame_join.state_reg;
  assign frame_ptr_reg = axis_frame_join.frame_ptr_reg;
  assign port_sel_reg = axis_frame_join.port_sel_reg;
  assign busy_reg = axis_frame_join.busy_reg;
  assign m_axis_tvalid_reg = axis_frame_join.m_axis_tvalid_reg;
  assign m_axis_tready_int_reg = axis_frame_join.m_axis_tready_int_reg;
  assign temp_m_axis_tvalid_reg = axis_frame_join.temp_m_axis_tvalid_reg;
  assign m_axis_tvalid_int = axis_frame_join.m_axis_tvalid_int;

  wire [DATA_WIDTH-1:0] input_tdata;
  wire input_tvalid;
  wire input_tlast;
  wire input_tuser;
  assign input_tdata = s_axis_tdata[port_sel_reg*DATA_WIDTH +: DATA_WIDTH];
  assign input_tvalid = s_axis_tvalid[port_sel_reg];
  assign input_tlast = s_axis_tlast[port_sel_reg];
  assign input_tuser = s_axis_tuser[port_sel_reg];

  wire [S_COUNT-1:0] ready_onehot_check;
  assign ready_onehot_check = s_axis_tready;

  wire ready_count_valid;
  assign ready_count_valid = (ready_onehot_check == 0) || 
                             ((ready_onehot_check & (ready_onehot_check - 1)) == 0);

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_state: assert(state_reg == STATE_IDLE);
      ap_reset_frame_ptr: assert(frame_ptr_reg == 0);
      ap_reset_port_sel: assert(port_sel_reg == 0);
      ap_reset_busy: assert(busy_reg == 0);
      ap_reset_ready: assert(s_axis_tready == 0);
      ap_reset_valid: assert(m_axis_tvalid_reg == 0);
    end

    if (!rst) begin
      ap_single_ready_assertion: assert(ready_count_valid);
    end

    if (!rst && state_reg == STATE_TRANSFER && port_sel_reg < S_COUNT) begin
      ap_ready_matches_port_selection: assert(
        (s_axis_tready == 0) || (s_axis_tready == (1 << port_sel_reg))
      );
    end

    if (!rst && $past(1'b1)) begin
      if ($past(m_axis_tvalid && !m_axis_tready)) begin
        ap_axis_valid_stable_until_ready: assert(m_axis_tvalid);
      end
    end

    if (!rst && state_reg == STATE_WRITE_TAG && TAG_ENABLE && TAG_WORD_WIDTH > 0) begin
      ap_tag_word_counter_bounds: assert(frame_ptr_reg < TAG_WORD_WIDTH);
    end

    if (!rst && state_reg == STATE_TRANSFER && S_COUNT > 0) begin
      ap_port_selector_bounds: assert(port_sel_reg < S_COUNT);
    end

    if (!rst && $past(1'b1) && TAG_ENABLE && TAG_WORD_WIDTH > 0) begin
      if ($past(state_reg == STATE_WRITE_TAG && frame_ptr_reg == TAG_WORD_WIDTH-1 && m_axis_tready_int_reg)) begin
        ap_tag_phase_completion: assert(state_reg == STATE_TRANSFER);
      end
    end

    if (!rst && $past(1'b1) && S_COUNT > 1) begin
      if ($past(state_reg == STATE_TRANSFER && input_tvalid && input_tlast && m_axis_tready_int_reg && port_sel_reg < S_COUNT-1)) begin
        ap_port_increment_on_tlast: assert(port_sel_reg == $past(port_sel_reg) + 1);
      end
    end

    if (!rst && m_axis_tlast && S_COUNT > 0) begin
      ap_tlast_only_on_final_port: assert(
        state_reg == STATE_TRANSFER && port_sel_reg == S_COUNT-1 && input_tlast
      );
    end

    if (!rst) begin
      ap_busy_reflects_non_idle: assert(busy_reg == (state_reg != STATE_IDLE));
    end

    if (!rst && state_reg == STATE_WRITE_TAG && TAG_ENABLE && TAG_WORD_WIDTH > 1) begin
      if (frame_ptr_reg < TAG_WORD_WIDTH-1) begin
        ap_no_ready_during_tag_write: assert(s_axis_tready == 0);
      end
    end

    if (!rst) begin
      ap_state_machine_valid_states: assert(
        (state_reg == STATE_IDLE) || 
        (state_reg == STATE_WRITE_TAG) || 
        (state_reg == STATE_TRANSFER)
      );
    end

    if (!rst && $past(1'b1) && TAG_ENABLE) begin
      if ($past(state_reg == STATE_IDLE && (|s_axis_tvalid))) begin
        ap_idle_to_tag_transition: assert(
          (state_reg == STATE_WRITE_TAG) || (state_reg == STATE_IDLE)
        );
      end
    end

    if (!rst && $past(1'b1) && S_COUNT > 0) begin
      if ($past(state_reg == STATE_TRANSFER && port_sel_reg == S_COUNT-1 && input_tlast && input_tvalid && m_axis_tready_int_reg)) begin
        ap_return_to_idle_on_completion: assert(state_reg == STATE_IDLE);
      end
    end
  end

endmodule

bind axis_frame_join axis_frame_join_spec #(
  .S_COUNT(S_COUNT),
  .DATA_WIDTH(DATA_WIDTH),
  .TAG_ENABLE(TAG_ENABLE),
  .TAG_WIDTH(TAG_WIDTH),
  .CL_S_COUNT(CL_S_COUNT),
  .TAG_WORD_WIDTH(TAG_WORD_WIDTH),
  .CL_TAG_WORD_WIDTH(CL_TAG_WORD_WIDTH),
  .STATE_IDLE(STATE_IDLE),
  .STATE_WRITE_TAG(STATE_WRITE_TAG),
  .STATE_TRANSFER(STATE_TRANSFER)
) spec_inst(.*);