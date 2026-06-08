5 print "{clr}"
10 t = 49152 : r = 49408
15 print "command: ",: input a$
30 for i = 1 to len(a$) : poke t+i, asc(mid$(a$,i,1)) : next
40 poke t, len(a$)
45 print "waiting.."

50 poke 1024,n
51 n = n + 1
52 if n > 200 then n=0: print "Pos (t):", peek(t)
58 if peek(r) = 0 then goto 50
59 print "msg received!"
60 n = peek(r)
70 for i = 1 to n : print chr$(peek(r+i)); : next : print
80 poke r, 0

90 goto 15
