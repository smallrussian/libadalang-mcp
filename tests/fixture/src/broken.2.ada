with Vehicle;

--  Deliberately broken: syntax error (missing semicolon and "end").
procedure Broken is
   X : Integer := 1
begin
   Vehicle.Log (X);
   if X > 0 then
      Vehicle.Log ("positive");
end Broken;
