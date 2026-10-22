with Vehicle;

--  Deliberately illegal: references a name that does not exist.
procedure Bad_Ref is
begin
   Vehicle.Log ("ok");
   Vehicle.Does_Not_Exist (42);
end Bad_Ref;
