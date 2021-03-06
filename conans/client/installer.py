import os
import time
import platform
import fnmatch
import shutil

from conans.paths import CONANINFO, BUILD_INFO, CONANENV, RUN_LOG_NAME
from conans.util.files import save, rmdir
from conans.model.ref import PackageReference
from conans.util.log import logger
from conans.errors import ConanException, format_conanfile_exception
from conans.client.packager import create_package
from conans.client.generators import write_generators, TXTGenerator
from conans.model.build_info import CppInfo
from conans.client.output import ScopedOutput
from conans.model.env_info import EnvInfo
from conans.client.source import config_source
from conans.client.generators.env import ConanEnvGenerator
from conans.tools import environment_append
from conans.util.tracer import log_package_built


def init_package_info(deps_graph, paths):
    """ Made external so it is independent of installer and can called
    in testing too
    """
    # Assign export root folders
    for node in deps_graph.nodes:
        conan_ref, conan_file = node
        if conan_ref:
            package_id = conan_file.info.package_id()
            package_reference = PackageReference(conan_ref, package_id)
            package_folder = paths.package(package_reference, conan_file.short_paths)
            conan_file.package_folder = package_folder
            conan_file.cpp_info = CppInfo(package_folder)
            conan_file.env_info = EnvInfo(package_folder)


class ConanInstaller(object):
    """ main responsible of retrieving binary packages or building them from source
    locally in case they are not found in remotes
    """
    def __init__(self, client_cache, user_io, remote_proxy):
        self._client_cache = client_cache
        self._out = user_io.out
        self._remote_proxy = remote_proxy

    def install(self, deps_graph, build_mode=False):
        """ given a DepsGraph object, build necessary nodes or retrieve them
        """
        self._deps_graph = deps_graph  # necessary for _build_package
        t1 = time.time()
        init_package_info(deps_graph, self._client_cache)
        # order by levels and propagate exports as download imports
        nodes_by_level = deps_graph.by_levels()
        logger.debug("Install-Process buildinfo %s" % (time.time() - t1))
        t1 = time.time()
        skip_private_nodes = self._compute_private_nodes(deps_graph, build_mode)
        logger.debug("Install-Process private %s" % (time.time() - t1))
        t1 = time.time()
        self._build(nodes_by_level, skip_private_nodes, build_mode)
        logger.debug("Install-build %s" % (time.time() - t1))

    def _compute_private_nodes(self, deps_graph, build_mode):
        """ computes a list of nodes that are not required to be built, as they are
        private requirements of already available shared libraries as binaries.

        If the package requiring a private node has an up to date binary package,
        the private node is not retrieved nor built
        """
        skip_nodes = set()  # Nodes that require private packages but are already built
        for node in deps_graph.nodes:
            conan_ref, conanfile = node
            if not [r for r in conanfile.requires.values() if r.private]:
                continue

            if conan_ref:
                build_forced = self._build_forced(conan_ref, build_mode, conanfile)
                if build_forced:
                    continue

                package_id = conanfile.info.package_id()
                package_reference = PackageReference(conan_ref, package_id)
                check_outdated = self._check_outdated(build_mode)

                if self._remote_proxy.package_available(package_reference,
                                                        conanfile.short_paths,
                                                        check_outdated):
                    skip_nodes.add(node)

        # Get the private nodes
        skippable_private_nodes = deps_graph.private_nodes(skip_nodes)
        return skippable_private_nodes

    def nodes_to_build(self, deps_graph, build_mode):
        """Called from info command when a build policy is used in build_order parameter"""
        # Get the nodes in order and if we have to build them
        nodes_by_level = deps_graph.by_levels()
        skip_private_nodes = self._compute_private_nodes(deps_graph, build_mode)
        nodes = self._get_nodes(nodes_by_level, skip_private_nodes, build_mode)
        return [(PackageReference(conan_ref, package_id), conan_file)
                for conan_ref, package_id, conan_file, build in nodes if build]

    def _build(self, nodes_by_level, skip_private_nodes, build_mode):
        """ The build assumes an input of conans ordered by degree, first level
        should be independent from each other, the next-second level should have
        dependencies only to first level conans.
        param nodes_by_level: list of lists [[nodeA, nodeB], [nodeC], [nodeD, ...], ...]

        build_mode => ["*"] if user wrote "--build"
                   => ["hello*", "bye*"] if user wrote "--build hello --build bye"
                   => False if user wrote "never"
                   => True if user wrote "missing"
                   => "outdated" if user wrote "--build outdated"

        """

        inverse = self._deps_graph.inverse_levels()
        flat = []

        for level in inverse:
            level = sorted(level, key=lambda x: x.conan_ref)
            flat.extend(level)

        # Get the nodes in order and if we have to build them
        nodes_to_process = self._get_nodes(nodes_by_level, skip_private_nodes, build_mode)

        for conan_ref, package_id, conan_file, build_needed in nodes_to_process:

            if build_needed:
                build_allowed = self._build_allowed(conan_ref, build_mode, conan_file)
                if not build_allowed:
                    self._raise_package_not_found_error(conan_ref, conan_file)

                output = ScopedOutput(str(conan_ref), self._out)
                package_ref = PackageReference(conan_ref, package_id)
                package_folder = self._client_cache.package(package_ref, conan_file.short_paths)
                if build_mode is True:
                    output.info("Building package from source as defined by build_policy='missing'")
                elif self._build_forced(conan_ref, build_mode, conan_file):
                    output.warn('Forced build from source')

                t1 = time.time()
                # Assign to node the propagated info
                self._propagate_info(conan_ref, conan_file, flat)

                # Call the conanfile's build method
                self._build_conanfile(conan_ref, conan_file, package_ref, package_folder, output)

                # Call the conanfile's package method
                self._package_conanfile(conan_ref, conan_file, package_ref, package_folder, output)

                # Call the info method
                self._package_info_conanfile(conan_ref, conan_file)

                duration = time.time() - t1
                log_file = os.path.join(self._client_cache.build(package_ref, conan_file.short_paths),
                                        RUN_LOG_NAME)
                log_file = log_file if os.path.exists(log_file) else None
                log_package_built(package_ref, duration, log_file)
            else:
                # Get the package, we have a not outdated remote package
                if conan_ref:
                    self._get_package(conan_ref, conan_file)

                # Assign to the node the propagated info
                # (conan_ref could be None if user project, but of course assign the info
                self._propagate_info(conan_ref, conan_file, flat)

                # Call the info method
                self._package_info_conanfile(conan_ref, conan_file)

    def _propagate_info(self, conan_ref, conan_file, flat):
        # Get deps_cpp_info from upstream nodes
        node_order = self._deps_graph.ordered_closure((conan_ref, conan_file), flat)
        public_deps = [name for name, req in conan_file.requires.items() if not req.private]
        conan_file.cpp_info.public_deps = public_deps
        for n in node_order:
            conan_file.deps_cpp_info.update(n.conanfile.cpp_info, n.conan_ref)
            conan_file.deps_env_info.update(n.conanfile.env_info, n.conan_ref)

    def _get_nodes(self, nodes_by_level, skip_nodes, build_mode):
        """Install the available packages if needed/allowed and return a list
        of nodes to build (tuples (conan_file, conan_ref))
        and installed nodes"""

        nodes_to_build = []
        # Now build each level, starting from the most independent one
        package_references = set()
        for level in nodes_by_level:
            for node in level:
                if node in skip_nodes:
                    continue
                conan_ref, conan_file = node

                # it is possible that the root conans
                # is not inside the storage but in a user folder, and thus its
                # treatment is different
                build_node = False
                package_id = None
                if conan_ref:
                    logger.debug("Processing node %s" % repr(conan_ref))
                    package_id = conan_file.info.package_id()
                    package_reference = PackageReference(conan_ref, package_id)
                    # Avoid processing twice the same package reference
                    if package_reference not in package_references:
                        package_references.add(package_reference)
                        check_outdated = self._check_outdated(build_mode)
                        if self._build_forced(conan_ref, build_mode, conan_file):
                            build_node = True
                        else:
                            build_node = not self._remote_proxy.package_available(package_reference,
                                                                                  conan_file.short_paths,
                                                                                  check_outdated)

                nodes_to_build.append((conan_ref, package_id, conan_file, build_node))

        return nodes_to_build

    def _get_package(self, conan_ref, conan_file):
        '''Get remote package. It won't check if it's outdated'''
        # Compute conan_file package from local (already compiled) or from remote
        output = ScopedOutput(str(conan_ref), self._out)
        package_id = conan_file.info.package_id()
        package_reference = PackageReference(conan_ref, package_id)

        conan_ref = package_reference.conan
        package_folder = self._client_cache.package(package_reference, conan_file.short_paths)

        # If already exists do not dirt the output, the common situation
        # is that package is already installed and OK. If don't, the proxy
        # will print some other message about it
        if not os.path.exists(package_folder):
            output.info("Installing package %s" % package_id)

        if self._remote_proxy.get_package(package_reference, short_paths=conan_file.short_paths):
            self._handle_system_requirements(conan_ref, package_reference, conan_file, output)
            return True

        self._raise_package_not_found_error(conan_ref, conan_file)

    def _build_conanfile(self, conan_ref, conan_file, package_reference, package_folder, output):
        """Calls the conanfile's build method"""

        build_folder = self._client_cache.build(package_reference, conan_file.short_paths)
        src_folder = self._client_cache.source(conan_ref, conan_file.short_paths)
        export_folder = self._client_cache.export(conan_ref)

        self._handle_system_requirements(conan_ref, package_reference, conan_file, output)

        with environment_append(conan_file.env):
            self._build_package(export_folder, src_folder, build_folder, package_folder, conan_file, output)

    def _package_conanfile(self, conan_ref, conan_file, package_reference, package_folder, output):
        """Generate the info txt files and calls the conanfile package method"""

        # FIXME: Is weak to assign here the recipe_hash
        conan_file.info.recipe_hash = self._client_cache.load_manifest(conan_ref).summary_hash
        build_folder = self._client_cache.build(package_reference, conan_file.short_paths)

        # Creating ***info.txt files
        save(os.path.join(build_folder, CONANINFO), conan_file.info.dumps())
        output.info("Generated %s" % CONANINFO)
        save(os.path.join(build_folder, BUILD_INFO), TXTGenerator(conan_file).content)
        output.info("Generated %s" % BUILD_INFO)
        save(os.path.join(build_folder, CONANENV), ConanEnvGenerator(conan_file).content)
        output.info("Generated %s" % CONANENV)

        os.chdir(build_folder)
        with environment_append(conan_file.env):
            create_package(conan_file, build_folder, package_folder, output)
            self._remote_proxy.handle_package_manifest(package_reference, installed=True)

    def _raise_package_not_found_error(self, conan_ref, conan_file):
        settings_text = ", ".join(conan_file.info.full_settings.dumps().splitlines())
        options_text = ", ".join(conan_file.info.full_options.dumps().splitlines())
        author_contact = " at '%s'" % conan_file.url if conan_file.url else ""

        raise ConanException('''Can't find a '%s' package for the specified options and settings

- Try to build from sources with "--build %s" parameter
- If it fails, you could try to contact the package author %s, report your configuration and try to collaborate to support it.

Package configuration:
- Settings: %s
- Options: %s''' % (conan_ref, conan_ref.name, author_contact, settings_text, options_text))

    def _handle_system_requirements(self, conan_ref, package_reference, conan_file, coutput):
        """ check first the system_reqs/system_requirements.txt existence, if not existing
        check package/sha1/
        """
        if "system_requirements" not in type(conan_file).__dict__:
            return

        system_reqs_path = self._client_cache.system_reqs(conan_ref)
        system_reqs_package_path = self._client_cache.system_reqs_package(package_reference)
        if os.path.exists(system_reqs_path) or os.path.exists(system_reqs_package_path):
            return

        output = self.call_system_requirements(conan_file, coutput)

        try:
            output = str(output or "")
        except:
            coutput.warn("System requirements didn't return a string")
            output = ""
        if getattr(conan_file, "global_system_requirements", None):
            save(system_reqs_path, output)
        else:
            save(system_reqs_package_path, output)

    def call_system_requirements(self, conan_file, output):
        try:
            return conan_file.system_requirements()
        except Exception as e:
            output.error("while executing system_requirements(): %s" % str(e))
            raise ConanException("Error in system requirements")

    def _build_package(self, export_folder, src_folder, build_folder, package_folder, conan_file, output):
        """ builds the package, creating the corresponding build folder if necessary
        and copying there the contents from the src folder. The code is duplicated
        in every build, as some configure processes actually change the source
        code
        """

        try:
            rmdir(build_folder)
            rmdir(package_folder)
        except Exception as e:
            raise ConanException("%s\n\nCouldn't remove folder, might be busy or open\n"
                                 "Close any app using it, and retry" % str(e))

        output.info('Building your package in %s' % build_folder)
        config_source(export_folder, src_folder, conan_file, output)
        output.info('Copying sources to build folder')

        def check_max_path_len(src, files):
            if platform.system() != "Windows":
                return []
            filtered_files = []
            for the_file in files:
                source_path = os.path.join(src, the_file)
                # Without storage path, just relative
                rel_path = os.path.relpath(source_path, src_folder)
                dest_path = os.path.normpath(os.path.join(build_folder, rel_path))
                # it is NOT that "/" is counted as "\\" so it counts double
                # seems a bug in python, overflows paths near the limit of 260,
                if len(dest_path) >= 249:
                    filtered_files.append(the_file)
                    output.warn("Filename too long, file excluded: %s" % dest_path)
            return filtered_files

        shutil.copytree(src_folder, build_folder, symlinks=True, ignore=check_max_path_len)
        logger.debug("Copied to %s" % build_folder)
        logger.debug("Files copied %s" % os.listdir(build_folder))
        os.chdir(build_folder)
        conan_file._conanfile_directory = build_folder
        # Read generators from conanfile and generate the needed files
        logger.debug("Writing generators")
        write_generators(conan_file, build_folder, output)
        logger.debug("Files copied after generators %s" % os.listdir(build_folder))

        # Build step might need DLLs, binaries as protoc to generate source files
        # So execute imports() before build, storing the list of copied_files
        from conans.client.importer import run_imports
        copied_files = run_imports(conan_file, build_folder, output)

        try:
            # This is necessary because it is different for user projects
            # than for packages
            conan_file._conanfile_directory = build_folder
            logger.debug("Call conanfile.build() with files in build folder: %s" % os.listdir(build_folder))
            conan_file.build()

            self._out.writeln("")
            output.success("Package '%s' built" % conan_file.info.package_id())
            output.info("Build folder %s" % build_folder)
        except Exception as e:
            os.chdir(src_folder)
            self._out.writeln("")
            output.error("Package '%s' build failed" % conan_file.info.package_id())
            output.warn("Build folder %s" % build_folder)
            raise ConanException("%s: %s" % (conan_file.name, str(e)))
        finally:
            conan_file._conanfile_directory = export_folder
            # Now remove all files that were imported with imports()
            for f in copied_files:
                try:
                    if(f.startswith(build_folder)):
                        os.remove(f)
                except Exception:
                    self._out.warn("Unable to remove imported file from build: %s" % f)

    def _package_info_conanfile(self, conan_ref, conan_file):
        # Once the node is build, execute package info, so it has access to the
        # package folder and artifacts
        try:
            conan_file.package_info()
        except Exception as e:
            msg = format_conanfile_exception(str(conan_ref), "package_info", e)
            raise ConanException(msg)

    def _build_forced(self, conan_ref, build_mode, conan_file):
        if build_mode == "outdated":
            return False

        if conan_file.build_policy_always:
            out = ScopedOutput(str(conan_ref), self._out)
            out.info("Building package from source as defined by build_policy='always'")
            return True

        if build_mode is False:  # "never" option, default
            return False

        if build_mode is True:  # Build missing (just if needed), not force
            return False

        # Patterns to match, if package matches pattern, build is forced
        force_build = any([fnmatch.fnmatch(str(conan_ref), pattern)
                           for pattern in build_mode])
        return force_build

    def _build_allowed(self, conan_ref, build_mode, conan_file):
        forced = self._build_forced(conan_ref, build_mode, conan_file)
        # Option "--build outdated" means: missing or outdated, so don't care if it's really outdated
        return forced or build_mode in (True, "outdated") or conan_file.build_policy_missing

    def _check_outdated(self, build_mode):
        return build_mode == "outdated"
