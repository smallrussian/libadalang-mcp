separate (Vehicle.Engine)
procedure Start (E : in out Engine) is
begin
   E.Running := True;
   Log ("engine started");
end Start;
