module fifo_sync #(
    parameter int DEPTH = 4,
    parameter int WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             wr_en,
    input  logic [WIDTH-1:0] wr_data,
    input  logic             rd_en,
    output logic [WIDTH-1:0] rd_data,
    output logic             full,
    output logic             empty
);
    localparam int PTR_W = $clog2(DEPTH);

    logic [WIDTH-1:0] mem [0:DEPTH-1];
    logic [PTR_W:0]   wr_ptr, rd_ptr;

    assign full  = (wr_ptr[PTR_W] != rd_ptr[PTR_W]) && (wr_ptr[PTR_W-1:0] == rd_ptr[PTR_W-1:0]);
    assign empty = (wr_ptr == rd_ptr);
    assign rd_data = mem[rd_ptr[PTR_W-1:0]];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= '0;
            rd_ptr <= '0;
        end else begin
            if (wr_en && !full)  begin mem[wr_ptr[PTR_W-1:0]] <= wr_data; wr_ptr <= wr_ptr + 1; end
            if (rd_en && !empty) rd_ptr <= rd_ptr + 1;
        end
    end
endmodule
