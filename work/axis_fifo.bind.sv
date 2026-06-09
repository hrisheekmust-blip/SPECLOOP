module axis_fifo_spec #(
  parameter DEPTH = 256,
  parameter DATA_WIDTH = 8,
  parameter KEEP_ENABLE = 0,
  parameter KEEP_WIDTH = 1,
  parameter LAST_ENABLE = 1,
  parameter ID_ENABLE = 0,
  parameter ID_WIDTH = 8,
  parameter DEST_ENABLE = 0,
  parameter DEST_WIDTH = 8,
  parameter USER_ENABLE = 1,
  parameter USER_WIDTH = 1,
  parameter RAM_PIPELINE = 1,
  parameter OUTPUT_FIFO_ENABLE = 0,
  parameter FRAME_FIFO = 0,
  parameter USER_BAD_FRAME_VALUE = 1'b1,
  parameter USER_BAD_FRAME_MASK = 1'b1,
  parameter DROP_OVERSIZE_FRAME = 0,
  parameter DROP_BAD_FRAME = 0,
  parameter DROP_WHEN_FULL = 0,
  parameter MARK_WHEN_FULL = 0,
  parameter PAUSE_ENABLE = 0,
  parameter FRAME_PAUSE = 0,
  parameter ADDR_WIDTH = 8,
  parameter CL_KEEP_WDITH = 0,
  parameter OUTPUT_FIFO_ADDR_WIDTH = 3,
  parameter KEEP_OFFSET = 8,
  parameter LAST_OFFSET = 8,
  parameter ID_OFFSET = 9,
  parameter DEST_OFFSET = 9,
  parameter USER_OFFSET = 9,
  parameter WIDTH = 10
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
  input logic pause_req,
  input logic pause_ack,
  input logic [$clog2(DEPTH):0] status_depth,
  input logic [$clog2(DEPTH):0] status_depth_commit,
  input logic status_overflow,
  input logic status_bad_frame,
  input logic status_good_frame
);

  wire [ADDR_WIDTH:0] wr_ptr_reg;
  wire [ADDR_WIDTH:0] wr_ptr_commit_reg;
  wire [ADDR_WIDTH:0] rd_ptr_reg;
  wire s_frame_reg;
  wire drop_frame_reg;
  wire mark_frame_reg;
  wire send_frame_reg;

  assign wr_ptr_reg = axis_fifo.wr_ptr_reg;
  assign wr_ptr_commit_reg = axis_fifo.wr_ptr_commit_reg;
  assign rd_ptr_reg = axis_fifo.rd_ptr_reg;
  assign s_frame_reg = axis_fifo.s_frame_reg;
  assign drop_frame_reg = axis_fifo.drop_frame_reg;
  assign mark_frame_reg = axis_fifo.mark_frame_reg;
  assign send_frame_reg = axis_fifo.send_frame_reg;

  wire full;
  wire empty;
  wire full_wr;

  assign full = (wr_ptr_reg == (rd_ptr_reg ^ {1'b1, {ADDR_WIDTH{1'b0}}}));
  assign empty = (wr_ptr_commit_reg == rd_ptr_reg);
  assign full_wr = (wr_ptr_reg == (wr_ptr_commit_reg ^ {1'b1, {ADDR_WIDTH{1'b0}}}));

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_wr_ptr: assert(wr_ptr_reg == 0);
      ap_reset_wr_ptr_commit: assert(wr_ptr_commit_reg == 0);
      ap_reset_rd_ptr: assert(rd_ptr_reg == 0);
      ap_reset_s_frame: assert(s_frame_reg == 0);
      ap_reset_drop_frame: assert(drop_frame_reg == 0);
      ap_reset_mark_frame: assert(mark_frame_reg == 0);
      ap_reset_send_frame: assert(send_frame_reg == 0);
    end

    if (!rst && $past(1'b1)) begin
      if ($past(rst)) begin
        ap_postreset_wr_ptr: assert(wr_ptr_reg == 0);
        ap_postreset_wr_ptr_commit: assert(wr_ptr_commit_reg == 0);
        ap_postreset_rd_ptr: assert(rd_ptr_reg == 0);
        ap_postreset_s_frame: assert(s_frame_reg == 0);
        ap_postreset_drop_frame: assert(drop_frame_reg == 0);
        ap_postreset_mark_frame: assert(mark_frame_reg == 0);
        ap_postreset_send_frame: assert(send_frame_reg == 0);
      end

      if (!$past(rst) && $past(s_axis_tvalid && s_axis_tready)) begin
        if (!FRAME_FIFO) begin
          if (!$past(drop_frame_reg && MARK_WHEN_FULL)) begin
            if (!($past(full || mark_frame_reg) && MARK_WHEN_FULL)) begin
              ap_write_ptr_increment: assert(wr_ptr_reg == ($past(wr_ptr_reg) + 1));
            end
          end
        end
      end

      if (!$past(rst) && FRAME_FIFO) begin
        if ($past(s_axis_tvalid && s_axis_tready && s_axis_tlast && !drop_frame_reg)) begin
          ap_commit_on_good_frame: assert(wr_ptr_commit_reg == ($past(wr_ptr_reg) + 1));
        end
      end

      if (!$past(rst) && FRAME_FIFO) begin
        if ($past(drop_frame_reg && s_axis_tvalid && s_axis_tready && s_axis_tlast)) begin
          ap_drop_frame_reset_wr_ptr: assert(wr_ptr_reg == $past(wr_ptr_commit_reg));
        end
      end

      if (!$past(rst) && LAST_ENABLE) begin
        if ($past(s_axis_tvalid && s_axis_tready)) begin
          ap_frame_tracking: assert(s_frame_reg == !$past(s_axis_tlast));
        end
      end

      if (!$past(rst)) begin
        if ($past(status_overflow)) begin
          ap_overflow_pulse: assert(!status_overflow);
        end
      end

      if (!$past(rst)) begin
        if (status_overflow) begin
          ap_overflow_condition: assert($past(full || drop_frame_reg || (full_wr && FRAME_FIFO)));
        end
      end

      if (!$past(rst) && !FRAME_FIFO && !DROP_WHEN_FULL && !MARK_WHEN_FULL) begin
        if (full) begin
          ap_tready_deassert_when_full: assert(!s_axis_tready);
        end
      end

      if (!$past(rst) && FRAME_FIFO) begin
        ap_wr_ptr_ge_commit: assert((wr_ptr_reg - wr_ptr_commit_reg) <= (1 << ADDR_WIDTH));
      end

      if (!$past(rst)) begin
        if ((rd_ptr_reg - wr_ptr_commit_reg) > (1 << ADDR_WIDTH)) begin
          ap_rd_ptr_never_exceeds_commit: assert(rd_ptr_reg == $past(rd_ptr_reg));
        end
      end

      if (!$past(rst) && $past(status_bad_frame)) begin
        ap_bad_frame_pulse: assert(!status_bad_frame);
      end

      if (!$past(rst) && $past(status_good_frame)) begin
        ap_good_frame_pulse: assert(!status_good_frame);
      end
    end
  end

endmodule

bind axis_fifo axis_fifo_spec #(
  .DEPTH(DEPTH),
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
  .RAM_PIPELINE(RAM_PIPELINE),
  .OUTPUT_FIFO_ENABLE(OUTPUT_FIFO_ENABLE),
  .FRAME_FIFO(FRAME_FIFO),
  .USER_BAD_FRAME_VALUE(USER_BAD_FRAME_VALUE),
  .USER_BAD_FRAME_MASK(USER_BAD_FRAME_MASK),
  .DROP_OVERSIZE_FRAME(DROP_OVERSIZE_FRAME),
  .DROP_BAD_FRAME(DROP_BAD_FRAME),
  .DROP_WHEN_FULL(DROP_WHEN_FULL),
  .MARK_WHEN_FULL(MARK_WHEN_FULL),
  .PAUSE_ENABLE(PAUSE_ENABLE),
  .FRAME_PAUSE(FRAME_PAUSE),
  .ADDR_WIDTH(ADDR_WIDTH),
  .CL_KEEP_WDITH(CL_KEEP_WDITH),
  .OUTPUT_FIFO_ADDR_WIDTH(OUTPUT_FIFO_ADDR_WIDTH),
  .KEEP_OFFSET(KEEP_OFFSET),
  .LAST_OFFSET(LAST_OFFSET),
  .ID_OFFSET(ID_OFFSET),
  .DEST_OFFSET(DEST_OFFSET),
  .USER_OFFSET(USER_OFFSET),
  .WIDTH(WIDTH)
) spec_inst (.*);
