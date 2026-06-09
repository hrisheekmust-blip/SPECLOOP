module axis_frame_length_adjust_spec #(
    parameter DATA_WIDTH = 8,
    parameter KEEP_ENABLE = 1'b0,
    parameter KEEP_WIDTH = 1,
    parameter ID_ENABLE = 0,
    parameter ID_WIDTH = 8,
    parameter DEST_ENABLE = 0,
    parameter DEST_WIDTH = 8,
    parameter USER_ENABLE = 1,
    parameter USER_WIDTH = 1
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
    input logic [USER_WIDTH-1:0] m_axis_tuser,
    input logic status_valid,
    input logic status_ready,
    input logic status_frame_pad,
    input logic status_frame_truncate,
    input logic [15:0] status_frame_length,
    input logic [15:0] status_frame_original_length,
    input logic [15:0] length_min,
    input logic [15:0] length_max
);

    localparam [2:0] STATE_IDLE = 3'd0;
    localparam [2:0] STATE_TRANSFER = 3'd1;
    localparam [2:0] STATE_PAD = 3'd2;
    localparam [2:0] STATE_TRUNCATE = 3'd3;

    wire [2:0] state_reg;
    wire [15:0] frame_ptr_reg;
    wire [15:0] short_counter_reg;
    wire [15:0] long_counter_reg;
    wire status_valid_reg;
    wire s_axis_tready_reg;
    wire m_axis_tvalid_reg;
    wire temp_m_axis_tvalid_reg;
    wire m_axis_tready_int_reg;

    assign state_reg = axis_frame_length_adjust.state_reg;
    assign frame_ptr_reg = axis_frame_length_adjust.frame_ptr_reg;
    assign short_counter_reg = axis_frame_length_adjust.short_counter_reg;
    assign long_counter_reg = axis_frame_length_adjust.long_counter_reg;
    assign status_valid_reg = axis_frame_length_adjust.status_valid_reg;
    assign s_axis_tready_reg = axis_frame_length_adjust.s_axis_tready_reg;
    assign m_axis_tvalid_reg = axis_frame_length_adjust.m_axis_tvalid_reg;
    assign temp_m_axis_tvalid_reg = axis_frame_length_adjust.temp_m_axis_tvalid_reg;
    assign m_axis_tready_int_reg = axis_frame_length_adjust.m_axis_tready_int_reg;

    always @(posedge clk) begin
        if (rst) begin
            ap_reset_state: assert(state_reg == STATE_IDLE);
            ap_reset_ready: assert(!s_axis_tready_reg);
            ap_reset_status: assert(!status_valid_reg);
            ap_reset_frame_ptr: assert(frame_ptr_reg == 16'd0);
        end
    end

    always @(posedge clk) begin
        if (!rst && $past(1'b1) && $past(s_axis_tvalid) && !$past(s_axis_tready) && s_axis_tvalid) begin
            ap_slave_data_stable: assert(s_axis_tdata == $past(s_axis_tdata));
            ap_slave_last_stable: assert(s_axis_tlast == $past(s_axis_tlast));
            ap_slave_keep_stable: assert(s_axis_tkeep == $past(s_axis_tkeep));
        end
    end

    always @(posedge clk) begin
        if (!rst && $past(1'b1) && $past(m_axis_tvalid) && !$past(m_axis_tready) && m_axis_tvalid) begin
            ap_master_data_stable: assert(m_axis_tdata == $past(m_axis_tdata));
            ap_master_last_stable: assert(m_axis_tlast == $past(m_axis_tlast));
            ap_master_keep_stable: assert(m_axis_tkeep == $past(m_axis_tkeep));
        end
    end

    always @(posedge clk) begin
        if (!rst && $past(1'b1) && $past(status_valid) && !$past(status_ready) && status_valid) begin
            ap_status_valid_stable: assert(status_valid);
        end
    end

    always @(posedge clk) begin
        if (!rst && status_valid && !status_frame_pad && !status_frame_truncate) begin
            ap_passthrough_length: assert(status_frame_length == status_frame_original_length);
        end
    end

    always @(posedge clk) begin
        if (!rst) begin
            ap_short_counter_bound: assert(short_counter_reg <= length_min);
        end
    end

    always @(posedge clk) begin
        if (!rst) begin
            ap_long_counter_bound: assert(long_counter_reg <= length_max);
        end
    end

    always @(posedge clk) begin
        if (!rst) begin
            ap_fsm_valid_states: assert(
                state_reg == STATE_IDLE ||
                state_reg == STATE_TRANSFER ||
                state_reg == STATE_PAD ||
                state_reg == STATE_TRUNCATE
            );
        end
    end

    always @(posedge clk) begin
        if (!rst && state_reg == STATE_PAD) begin
            ap_pad_no_input: assert(!s_axis_tready);
        end
    end

    always @(posedge clk) begin
        if (!rst && state_reg == STATE_TRUNCATE && s_axis_tvalid && !s_axis_tlast) begin
            ap_truncate_no_output: assert(!m_axis_tvalid || !m_axis_tready_int_reg);
        end
    end

    always @(posedge clk) begin
        if (!rst && $past(1'b1) && $past(state_reg) != STATE_IDLE && state_reg == STATE_IDLE) begin
            ap_frame_ptr_reset: assert(frame_ptr_reg == 16'd0);
        end
    end

    always @(posedge clk) begin
        if (!rst && $past(1'b1)) begin
            if ($past(axis_frame_length_adjust.m_axis_tvalid_int) && !$past(m_axis_tready_int_reg) && !$past(m_axis_tready)) begin
                ap_output_pipeline_temp: assert(temp_m_axis_tvalid_reg);
            end
        end
    end

endmodule

bind axis_frame_length_adjust axis_frame_length_adjust_spec #(
    .DATA_WIDTH(DATA_WIDTH),
    .KEEP_ENABLE(KEEP_ENABLE),
    .KEEP_WIDTH(KEEP_WIDTH),
    .ID_ENABLE(ID_ENABLE),
    .ID_WIDTH(ID_WIDTH),
    .DEST_ENABLE(DEST_ENABLE),
    .DEST_WIDTH(DEST_WIDTH),
    .USER_ENABLE(USER_ENABLE),
    .USER_WIDTH(USER_WIDTH)
) spec_inst(.*);