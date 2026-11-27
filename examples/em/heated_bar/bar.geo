// Dimensions in millimetres. The untagged side faces have zero normal heat flux.
SetFactory("OpenCASCADE");
Box(1) = {0, 0, 0, 2, 0.5, 0.5};
eps = 1e-6;
left() = Surface In BoundingBox {-eps, -eps, -eps, eps, 0.5+eps, 0.5+eps};
right() = Surface In BoundingBox {2-eps, -eps, -eps, 2+eps, 0.5+eps, 0.5+eps};
Physical Surface("left") = {left()};
Physical Surface("right") = {right()};
Physical Volume("bar") = {1};
Mesh.MeshSizeMin = 0.08;
Mesh.MeshSizeMax = 0.08;
Mesh.MshFileVersion = 4.1;
