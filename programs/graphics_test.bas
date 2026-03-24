10 rem graphics mode test
20 rem set screen to black, border to blue
30 poke 53280,6:poke 53281,0
40 rem enable bitmap mode
50 rem $d011 bit 5 = bitmap mode
60 poke 53265,peek(53265) or 32
70 rem set bitmap at $2000, screen at $0400
80 poke 53272,8
90 rem clear bitmap memory
100 for i=8192 to 16191:poke i,0:next
110 rem set screen ram colors (white on black)
120 for i=1024 to 2023:poke i,16:next
130 rem draw some pixels
140 for i=8192 to 8192+319:poke i,255:next
150 rem draw vertical lines
160 for y=0 to 199
170 for x=0 to 7
180 byte=8192+y*40+x
190 poke byte,255
200 next x
210 next y
220 rem wait for keypress
230 get k$:if k$="" then 230
240 rem restore text mode
250 poke 53265,peek(53265) and 223
