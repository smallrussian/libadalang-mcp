with Vehicle.Engine;

package body Garage is

   procedure Park (S : in out Slot; E : access Vehicle.Engine.Engine) is
   begin
      S.Occupant := E;
   end Park;

end Garage;
