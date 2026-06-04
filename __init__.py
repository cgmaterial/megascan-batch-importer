bl_info = {
    "name": "Megascan Batch Importer",
    "author": "CGMaterial",
    "version": (1, 0),
    "blender": (5, 1, 0),
    "location": "View3D > N-Panel > Import Megascans",
    "description": "Batch import megascan collection to blender and mark as assets",
    "category": "Import-Export",
}

import bpy
import os
import zipfile
import uuid
import mathutils
import tempfile
import threading


def ensure_node_wrangler():
    """Ensures that the Node Wrangler addon is enabled."""
    if "node_wrangler" not in bpy.context.preferences.addons:
        bpy.ops.preferences.addon_enable(module="node_wrangler")


def clear_scene_data():
    """Cleans out old meshes, materials, and textures before processing a new zip."""
    if bpy.context.view_layer.objects.active and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

    data_blocks = [
        bpy.data.meshes, bpy.data.materials, bpy.data.images,
        bpy.data.cameras, bpy.data.textures, bpy.data.curves
    ]
    for block in data_blocks:
        for item in list(block):
            block.remove(item, do_unlink=True)

    for col in list(bpy.data.collections):
        if col != bpy.context.scene.collection:
            bpy.data.collections.remove(col)


def ensure_catalogs(library_path, zip_name):
    """Ensures that asset catalogs exist using the standard Blender format."""
    cats_file = os.path.join(library_path, "blender_assets.cats.txt")
    mesh_path, mat_path = f"{zip_name}/mesh", f"{zip_name}/materials"
    existing_entries = {}

    if os.path.exists(cats_file):
        try:
            with open(cats_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#') or not line.strip() or "VERSION" in line:
                        continue
                    parts = line.strip().split(':')
                    if len(parts) >= 3:
                        existing_entries[parts[1]] = parts[0]
        except Exception as e:
            print(f"Warning parsing existing catalog file: {e}")

    main_uuid = existing_entries.setdefault(zip_name, str(uuid.uuid4()))
    mesh_uuid = existing_entries.setdefault(mesh_path, str(uuid.uuid4()))
    mat_uuid = existing_entries.setdefault(mat_path, str(uuid.uuid4()))

    try:
        with open(cats_file, 'w', encoding='utf-8') as f:
            f.write("# Blender Asset Catalog Definition File\nVERSION 1\n\n")
            for path, uid in existing_entries.items():
                f.write(f"{uid}:{path}:{path.split('/')[-1]}\n")
    except Exception as e:
        print(f"Error writing catalog definitions file: {e}")

    return mesh_uuid, mat_uuid


def setup_material_with_node_wrangler(obj, mat, folder_path, texture_filenames):
    """Executes Node Wrangler and configures AO & Displacement maps."""
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links

    principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None) or nodes.new(
        type='ShaderNodeBsdfPrincipled')

    for n in nodes:
        n.select = False
    principled.select = True
    nodes.active = principled

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    node_area = next((a for a in bpy.context.screen.areas if a.type == 'NODE_EDITOR'), None)
    restore_area_type = None

    if not node_area and bpy.context.screen and bpy.context.screen.areas:
        node_area = bpy.context.screen.areas[0]
        restore_area_type = node_area.type
        node_area.type = 'NODE_EDITOR'
        node_area.ui_type = 'ShaderNodeTree'

    if not node_area:
        return

    node_area.spaces.active.node_tree = mat.node_tree
    window_region = next((r for r in node_area.regions if r.type == 'WINDOW'), None) or node_area.regions[0]

    try:
        with bpy.context.temp_override(
                window=bpy.context.window, screen=bpy.context.screen,
                area=node_area, region=window_region, space_data=node_area.spaces.active
        ):
            bpy.ops.node.nw_add_textures_for_principled(
                filepath=os.path.join(folder_path, texture_filenames[0]),
                directory=folder_path if folder_path.endswith(os.sep) else folder_path + os.sep,
                files=[{"name": f} for f in texture_filenames], relative_path=False
            )
    except Exception as e:
        print(f"Node Wrangler failed for {mat.name}: {e}")
    finally:
        if restore_area_type:
            node_area.type = restore_area_type

    try:
        mat.displacement_method = 'BOTH'
    except AttributeError:
        pass

    ao_filename = next((f for f in texture_filenames if "ao" in f.lower() or "ambientocclusion" in f.lower()), None)
    base_color_input = principled.inputs.get('Base Color')

    if ao_filename and base_color_input and base_color_input.is_linked:
        base_color_node = base_color_input.links[0].from_node
        ao_node = next((n for n in nodes if n.type == 'TEX_IMAGE' and n.image and (
                    os.path.basename(n.image.filepath).lower() == ao_filename.lower() or "ao" in n.image.name.lower())),
                       None)

        if not ao_node:
            ao_node = nodes.new(type='ShaderNodeTexImage')
            ao_node.label = "Ambient Occlusion"
            try:
                ao_node.image = bpy.data.images.load(os.path.join(folder_path, ao_filename), check_existing=True)
            except Exception as e:
                print(f"Could not load standalone AO map: {e}")

            if base_color_node.inputs['Vector'].is_linked:
                links.new(base_color_node.inputs['Vector'].links[0].from_output, ao_node.inputs['Vector'])

        if ao_node.image:
            ao_node.image.colorspace_settings.name = 'Non-Color'

        mix_node = nodes.new(type='ShaderNodeMix')
        mix_node.data_type, mix_node.blend_type = 'RGBA', 'MULTIPLY'

        input_factor = mix_node.inputs.get('Factor') or mix_node.inputs[0]
        input_A = mix_node.inputs.get('A') or mix_node.inputs[4]
        input_B = mix_node.inputs.get('B') or mix_node.inputs[5]
        input_factor.default_value = 1.0

        mix_node.location = (principled.location.x - 250, principled.location.y)
        ao_node.location = (mix_node.location.x - 320, mix_node.location.y + 160)
        base_color_node.location = (mix_node.location.x - 320, mix_node.location.y - 120)

        old_color_output = base_color_node.outputs['Color']
        links.remove(base_color_input.links[0])
        links.new(ao_node.outputs['Color'], input_A)
        links.new(old_color_output, input_B)
        links.new(mix_node.outputs.get('Result') or mix_node.outputs[0], base_color_input)


def render_custom_thumbnail(id_block, target_obj, save_path, is_material=False):
    """Isolates target asset and performs a high-speed optimized preview render snapshot."""
    scene = bpy.context.scene
    eevee = getattr(scene, "eevee", None)

    # Cache original settings
    orig_settings = {
        "engine": scene.render.engine, "res_x": scene.render.resolution_x, "res_y": scene.render.resolution_y,
        "filepath": scene.render.filepath, "camera": scene.camera, "transparent": scene.render.film_transparent,
        "world": scene.world, "compositing": scene.render.use_compositing, "sequencer": scene.render.use_sequencer,
        "raytracing": getattr(eevee, "use_raytracing", False), "gtao": getattr(eevee, "use_gtao", False),
        "samples": getattr(eevee, "render_samples", getattr(eevee, "taa_render_samples", 64))
    }

    # Apply High-Speed Render Optimizations
    scene.render.engine = 'BLENDER_EEVEE'
    scene.render.resolution_x, scene.render.resolution_y = 256, 256
    scene.render.film_transparent = True
    scene.render.use_compositing, scene.render.use_sequencer = False, False

    if eevee:
        if hasattr(eevee, "use_raytracing"): eevee.use_raytracing = False
        if hasattr(eevee, "use_gtao"): eevee.use_gtao = False
        if hasattr(eevee, "render_samples"):
            eevee.render_samples = 1
        elif hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = 1

    # Ambient Setup
    temp_world = bpy.data.worlds.new("Temp_Thumbnail_World")
    scene.world = temp_world
    temp_world.use_nodes = True
    bg_node = temp_world.node_tree.nodes.get('Background')
    if bg_node:
        bg_node.inputs['Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        bg_node.inputs['Strength'].default_value = 1.2

    # Isolate Asset Visibility
    hidden_objects = []
    for o in scene.objects:
        if o != target_obj and not o.hide_render:
            o.hide_render, o.hide_viewport = True, True
            hidden_objects.append(o)

    # Frame Layout Rigging
    bbox_corners = [target_obj.matrix_world @ mathutils.Vector(corner) for corner in target_obj.bound_box]
    center = sum(bbox_corners, mathutils.Vector()) / 8.0
    max_dim = max(target_obj.dimensions) or 1.0

    bpy.ops.object.camera_add()
    cam = bpy.context.active_object
    scene.camera = cam

    if is_material:
        cam.location = center + mathutils.Vector((0.0, 0.0, max_dim * 1.1))
        cam.rotation_euler = (0.0, 0.0, 0.0)
    else:
        cam.location = center + mathutils.Vector((max_dim * 1.2, -max_dim * 1.2, max_dim * 0.8))
        track_empty = bpy.data.objects.new("TrackTarget", None)
        scene.collection.objects.link(track_empty)
        track_empty.location = center
        track_constraint = cam.constraints.new(type='TRACK_TO')
        track_constraint.target, track_constraint.track_axis, track_constraint.up_axis = track_empty, 'TRACK_NEGATIVE_Z', 'UP_Y'

    bpy.context.view_layer.update()
    scene.render.filepath = save_path
    bpy.ops.render.render(write_still=True)

    with bpy.context.temp_override(id=id_block):
        bpy.ops.ed.lib_id_load_custom_preview(filepath=save_path)

    # Cleanup Rigging
    bpy.data.objects.remove(cam, do_unlink=True)
    if not is_material: bpy.data.objects.remove(track_empty, do_unlink=True)
    bpy.data.worlds.remove(temp_world)

    # Restore Settings
    for o in hidden_objects:
        o.hide_render, o.hide_viewport = False, False

    scene.render.engine = orig_settings["engine"]
    scene.render.resolution_x, scene.render.resolution_y = orig_settings["res_x"], orig_settings["res_y"]
    scene.render.filepath, scene.camera = orig_settings["filepath"], orig_settings["camera"]
    scene.render.film_transparent, scene.world = orig_settings["transparent"], orig_settings["world"]
    scene.render.use_compositing, scene.render.use_sequencer = orig_settings["compositing"], orig_settings["sequencer"]

    if eevee:
        if hasattr(eevee, "use_raytracing"): eevee.use_raytracing = orig_settings["raytracing"]
        if hasattr(eevee, "use_gtao"): eevee.use_gtao = orig_settings["gtao"]
        if hasattr(eevee, "render_samples"):
            eevee.render_samples = orig_settings["samples"]
        elif hasattr(eevee, "taa_render_samples"):
            eevee.taa_render_samples = orig_settings["samples"]


class WM_OT_megascan_batch_processor(bpy.types.Operator):
    bl_idname = "wm.megascan_batch_processor"
    bl_label = "Batch Process Megascans"

    _timer = None
    zip_files = []
    current_index = 0
    source_dir = ""
    processing_state = 'START_ARCHIVE'

    # Non-blocking context state variables
    _extract_thread = None
    _task_queue = []
    grid_x = 0
    grid_y = 0
    spacing = 6.0
    items_per_row = 6
    temp_thumb_dir = ""
    mesh_uuid = ""
    mat_uuid = ""

    def modal(self, context, event):
        scene = context.scene

        if event.type == 'ESC':
            self.cancel_cleanup(scene)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            if self.current_index >= len(self.zip_files):
                self.success_finalize(scene)
                return {'FINISHED'}

            zip_filename = self.zip_files[self.current_index]
            zip_name = os.path.splitext(zip_filename)[0]

            # STEP 1: Multi-threaded Async Unzipping
            if self.processing_state == 'START_ARCHIVE':
                scene.megascan_status = f"Extracting Archive: {zip_filename}..."
                zip_path = os.path.join(self.source_dir, zip_filename)
                extract_dir = os.path.join(self.source_dir, zip_name)

                if not os.path.exists(extract_dir):
                    def extract_zip():
                        try:
                            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                zip_ref.extractall(extract_dir)
                        except Exception as e:
                            print(f"Extraction error: {e}")

                    self._extract_thread = threading.Thread(target=extract_zip)
                    self._extract_thread.start()
                    self.processing_state = 'CHECK_EXTRACT'
                else:
                    self.processing_state = 'PREPARE_SCENE'

            # STEP 2: Wait for extraction without hanging UI
            elif self.processing_state == 'CHECK_EXTRACT':
                if self._extract_thread and self._extract_thread.is_alive():
                    return {'RUNNING_MODAL'}
                self.processing_state = 'PREPARE_SCENE'

            # STEP 3: Setup Scene & Task Management Queue
            elif self.processing_state == 'PREPARE_SCENE':
                extract_dir = os.path.join(self.source_dir, zip_name)
                target_blend_path = os.path.join(self.source_dir, f"{zip_name}.blend")

                clear_scene_data()

                asset_libraries = bpy.context.preferences.filepaths.asset_libraries
                if "Megascans" not in asset_libraries:
                    asset_libraries.new(name="Megascans").path = self.source_dir
                else:
                    asset_libraries["Megascans"].path = self.source_dir

                self.mesh_uuid, self.mat_uuid = ensure_catalogs(self.source_dir, zip_name)

                try:
                    bpy.ops.wm.save_as_mainfile(filepath=target_blend_path)
                except Exception:
                    self.current_index += 1
                    self.processing_state = 'START_ARCHIVE'
                    return {'RUNNING_MODAL'}

                # Build precise isolated step processing loops
                self._task_queue = []
                for root, _, files in os.walk(extract_dir):
                    if not files: continue
                    fbx_files = [f for f in files if f.lower().endswith('.fbx')]
                    texture_files = [f for f in files if
                                     f.lower().endswith(('.jpg', '.jpeg', '.png', '.tga', '.tif', '.tiff', '.bmp'))]
                    folder_name = os.path.basename(root)
                    if fbx_files or texture_files:
                        self._task_queue.append((root, fbx_files, texture_files, folder_name))

                self.grid_x, self.grid_y = 0, 0
                self.temp_thumb_dir = tempfile.gettempdir()
                self.processing_state = 'PROCESS_QUEUE'

            # STEP 4: Process and Render exactly ONE item slice per modal frame
            elif self.processing_state == 'PROCESS_QUEUE':
                if self._task_queue:
                    root, fbx_files, texture_files, folder_name = self._task_queue.pop(0)
                    scene.megascan_status = f"[{zip_name}] processing layout element: {folder_name}"
                    self.process_single_task_item(root, fbx_files, texture_files, folder_name)
                else:
                    self.processing_state = 'SAVE_ARCHIVE'

            # STEP 5: Finalize main database block
            elif self.processing_state == 'SAVE_ARCHIVE':
                scene.megascan_status = f"Saving Container: {zip_name}.blend"
                try:
                    bpy.ops.wm.save_mainfile()
                except Exception:
                    pass
                self.current_index += 1
                scene.megascan_progress = int((self.current_index / len(self.zip_files)) * 100)
                self.processing_state = 'START_ARCHIVE'

        return {'RUNNING_MODAL'}

    def process_single_task_item(self, root, fbx_files, texture_files, folder_name):
        """Processes a single task asset unit to offload computational strain per cycle frame."""
        if fbx_files:
            for fbx in fbx_files:
                bpy.ops.object.select_all(action='DESELECT')
                try:
                    bpy.ops.wm.fbx_import(filepath=os.path.join(root, fbx))
                except AttributeError:
                    bpy.ops.import_scene.fbx(filepath=os.path.join(root, fbx))

                imported_mesh_objs = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
                if imported_mesh_objs:
                    for obj in imported_mesh_objs: obj.select_set(True)
                    bpy.context.view_layer.objects.active = imported_mesh_objs[0]
                    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

                for obj in imported_mesh_objs:
                    if obj.data:
                        obj.name = obj.data.name.lstrip("Aset_")[:-16]
                    obj.location = (self.grid_x * self.spacing, self.grid_y * self.spacing, 0)

                    if texture_files:
                        mat_name = f"Mat_{obj.name}"
                        mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
                        if obj.data.materials:
                            obj.data.materials[0] = mat
                        else:
                            obj.data.materials.append(mat)
                        setup_material_with_node_wrangler(obj, mat, root, texture_files)

                    obj.asset_mark()
                    if self.mesh_uuid: obj.asset_data.catalog_id = self.mesh_uuid

                    try:
                        render_custom_thumbnail(obj, obj, os.path.join(self.temp_thumb_dir, f"t_mesh_{obj.name}.png"),
                                                is_material=False)
                    except Exception as err:
                        print(f"Fallback to auto-preview on mesh {obj.name}: {err}")
                        obj.asset_generate_preview()

            self.grid_x += 1
            if self.grid_x >= self.items_per_row: self.grid_x = 0; self.grid_y += 1

        elif texture_files and not fbx_files:
            bpy.ops.mesh.primitive_plane_add(size=2)
            plane_obj = bpy.context.view_layer.objects.active
            plane_obj.name = f"Plane_{folder_name}"
            plane_obj.location = (self.grid_x * self.spacing, self.grid_y * self.spacing, 0)

            mat_name = f"Mat_{folder_name}"
            mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
            if plane_obj.data.materials:
                plane_obj.data.materials[0] = mat
            else:
                plane_obj.data.materials.append(mat)

            setup_material_with_node_wrangler(plane_obj, mat, root, texture_files)

            mat.asset_mark()
            if self.mat_uuid: mat.asset_data.catalog_id = self.mat_uuid

            try:
                render_custom_thumbnail(mat, plane_obj, os.path.join(self.temp_thumb_dir, f"t_mat_{mat.name}.png"),
                                        is_material=True)
            except Exception as err:
                print(f"Fallback to auto-preview on material {mat.name}: {err}")
                mat.asset_generate_preview()

            self.grid_x += 1
            if self.grid_x >= self.items_per_row: self.grid_x = 0; self.grid_y += 1

    def execute(self, context):
        scene = context.scene
        self.source_dir = scene.megascan_folder_path

        if not os.path.exists(self.source_dir) or not os.path.isdir(self.source_dir):
            self.report({'ERROR'}, "Invalid path directory configured.")
            return {'CANCELLED'}

        self.zip_files = [f for f in os.listdir(self.source_dir) if f.lower().endswith('.zip') and not os.path.exists(
            os.path.join(self.source_dir, f"{os.path.splitext(f)[0]}.blend"))]

        if not self.zip_files:
            self.report({'INFO'}, "All detected packages are already compiled.")
            return {'CANCELLED'}

        self.current_index = 0
        self.processing_state = 'START_ARCHIVE'
        scene.megascan_is_running, scene.megascan_progress = True, 0

        ensure_node_wrangler()
        self._timer = context.window_manager.event_timer_add(0.02, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel_cleanup(self, scene):
        if self._timer:
            bpy.context.window_manager.event_timer_remove(self._timer)
        scene.megascan_is_running = False
        scene.megascan_status = "Cancelled Operations"
        self.report({'WARNING'}, "Process stopped by user request.")

    def success_finalize(self, scene):
        if self._timer:
            bpy.context.window_manager.event_timer_remove(self._timer)
        scene.megascan_is_running, scene.megascan_progress = False, 100
        scene.megascan_status = "Completed Successfully!"
        try:
            bpy.ops.asset.library_refresh()
        except Exception:
            pass
        self.report({'INFO'}, "Batch Processing finished successfully.")


class VIEW3D_PT_megascan_batch_panel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Megascans'
    bl_label = "Megascan Database Compiler"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        is_processing = scene.megascan_is_running

        col = layout.column(align=True)
        col.enabled = not is_processing
        col.label(text="Target Download Folder Directory:")
        col.prop(scene, "megascan_folder_path", text="")

        layout.separator()

        if not is_processing:
            layout.operator("wm.megascan_batch_processor", text="Start Batch Process", icon='PLAY')
        else:
            layout.label(text="Status Monitor:")
            box = layout.box()
            box.label(text=scene.megascan_status, icon='INFO')
            layout.progress(factor=scene.megascan_progress / 100.0, text=f"{scene.megascan_progress}%")
            layout.separator()
            layout.label(text="Press ESC inside Viewport to cancel.", icon='CANCEL')


classes = (WM_OT_megascan_batch_processor, VIEW3D_PT_megascan_batch_panel)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.megascan_folder_path = bpy.props.StringProperty(
        name="Source Folder", description="Path to directory containing asset archives",
        default=r"", subtype='DIR_PATH'
    )
    bpy.types.Scene.megascan_progress = bpy.props.IntProperty(name="Progress Percentage", default=0, min=0, max=100)
    bpy.types.Scene.megascan_status = bpy.props.StringProperty(name="Current Status", default="Idle")
    bpy.types.Scene.megascan_is_running = bpy.props.BoolProperty(name="Engine Running Status State", default=False)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.megascan_folder_path
    del bpy.types.Scene.megascan_progress
    del bpy.types.Scene.megascan_status
    del bpy.types.Scene.megascan_is_running


if __name__ == "__main__":
    register()