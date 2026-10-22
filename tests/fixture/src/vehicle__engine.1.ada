--  Child package of Vehicle.
package Vehicle.Engine is

   type Engine is tagged record
      Running : Boolean := False;
      Current : Speed := 0;
      Where   : Position;
   end record;

   procedure Start (E : in out Engine);
   procedure Accelerate (E : in out Engine; By : Speed);
   function Is_Running (E : Engine) return Boolean;

end Vehicle.Engine;
