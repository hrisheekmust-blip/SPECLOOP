module top_compose #(
    parameter int CNT_WIDTH  = 8,
    parameter int FIFO_DEPTH = 4
) (
    input  logic                   clk,
    input  logic                   rst_n,
    input  logic                   cnt_en,
    input  logic                   fifo_wr,
    input  logic                   fifo_rd,
    output logic [CNT_WIDTH-1:0]   count,
    output logic [CNT_WIDTH-1:0]   rd_data,
    output logic                   full,
    output logic                   empty
);
    counter #(.WIDTH(CNT_WIDTH)) u_counter (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (cnt_en),
        .count (count)
    );

    fifo_sync #(.DEPTH(FIFO_DEPTH), .WIDTH(CNT_WIDTH)) u_fifo (
        .clk     (clk),
        .rst_n   (rst_n),
        .wr_en   (fifo_wr),
        .wr_data (count),
        .rd_en   (fifo_rd),
        .rd_data (rd_data),
        .full    (full),
        .empty   (empty)
    );
endmodule
