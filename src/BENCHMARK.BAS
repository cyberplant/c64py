10 rem c64py benchmark program
20 rem tests screen output, colors, and basic operations
30 print chr$(147)
40 print "c64py benchmark v1.0"
50 print "===================="
60 print
100 rem test 1: screen fill
110 print "test 1: screen fill..."
120 ti$="000000"
130 for i=1 to 10
140 print chr$(147);
150 for j=1 to 23
160 print "****************************************";
170 next j
180 next i
190 t1=ti
200 print chr$(147)
210 print "screen fill: ";t1;" jiffies"
300 rem test 2: color changes
310 print "test 2: color changes..."
320 ti$="000000"
330 for i=1 to 100
340 poke 53280,i and 15
350 poke 53281,i and 15
360 next i
370 t2=ti
380 poke 53280,14:poke 53281,6
390 print "color changes: ";t2;" jiffies"
500 rem test 3: math operations
510 print "test 3: math operations..."
520 ti$="000000"
530 for i=1 to 50
540 a=i*2+i/2-i
550 b=sqr(i)
560 c=sin(i/100)
570 next i
580 t3=ti
590 print "math ops: ";t3;" jiffies"
700 rem test 4: memory pokes
710 print "test 4: memory operations..."
720 ti$="000000"
730 for i=1 to 100
740 poke 1024+i,i and 255
750 poke 55296+i,i and 15
760 next i
770 t4=ti
780 print "memory ops: ";t4;" jiffies"
900 rem summary
910 print
920 print "===================="
930 tt=t1+t2+t3+t4
940 print "total: ";tt;" jiffies"
950 print "(";tt/60;" seconds)"
960 print "benchmark complete!"
999 end
