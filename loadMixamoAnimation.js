import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { mixamoVRMRigMap } from './mixamoVRMRigMap.js';

/**
 * Load Mixamo animation, convert for three-vrm use, and return it.
 * Supports both "mixamorigBone" and "mixamorig:Bone" naming conventions.
 *
 * @param {string} url A url of mixamo animation data
 * @param {VRM} vrm A target VRM
 * @returns {Promise<THREE.AnimationClip>} The converted AnimationClip
 */
export function loadMixamoAnimation( url, vrm ) {

	const loader = new FBXLoader();
	return loader.loadAsync( url ).then( ( asset ) => {

		let clip = THREE.AnimationClip.findByName( asset.animations, 'mixamo.com' );
		if ( !clip && asset.animations && asset.animations.length > 0 ) {
			clip = asset.animations[ 0 ];
		}
		if ( !clip ) {
			throw new Error( 'No animation found in FBX asset: ' + url );
		}

		const tracks = []; // KeyframeTracks compatible with VRM will be added here

		const restRotationInverse = new THREE.Quaternion();
		const parentRestWorldRotation = new THREE.Quaternion();
		const _quatA = new THREE.Quaternion();
		const _vec3 = new THREE.Vector3();

		// Find hips node (can be 'mixamorigHips' or 'mixamorig:Hips')
		const hipsNode = asset.getObjectByName( 'mixamorigHips' ) || asset.getObjectByName( 'mixamorig:Hips' );
		const motionHipsHeight = hipsNode ? hipsNode.position.y : 1;
		const vrmHipsHeight = vrm.humanoid?.normalizedRestPose?.hips?.position?.[ 1 ] || 1;
		const hipsPositionScale = motionHipsHeight > 0 ? ( vrmHipsHeight / motionHipsHeight ) : 1;

		clip.tracks.forEach( ( track ) => {

			const trackSplitted = track.name.split( '.' );
			const rawRigName = trackSplitted[ 0 ];
			// Normalize to clean key without colon (e.g. "mixamorig:Hips" -> "mixamorigHips")
			const normalizedRigName = rawRigName.replace( 'mixamorig:', 'mixamorig' );
			const vrmBoneName = mixamoVRMRigMap[ rawRigName ] || mixamoVRMRigMap[ normalizedRigName ];
			const vrmNode = vrm.humanoid?.getNormalizedBoneNode( vrmBoneName );
			const vrmNodeName = vrmNode?.name;
			const mixamoRigNode = asset.getObjectByName( rawRigName ) || asset.getObjectByName( normalizedRigName );

			if ( vrmNodeName != null && mixamoRigNode != null ) {

				const propertyName = trackSplitted[ 1 ];

				// Store rotations of rest-pose.
				mixamoRigNode.getWorldQuaternion( restRotationInverse ).invert();
				if ( mixamoRigNode.parent ) {
					mixamoRigNode.parent.getWorldQuaternion( parentRestWorldRotation );
				} else {
					parentRestWorldRotation.identity();
				}

				if ( track instanceof THREE.QuaternionKeyframeTrack ) {

					// Retarget rotation of mixamoRig to NormalizedBone.
					for ( let i = 0; i < track.values.length; i += 4 ) {

						const flatQuaternion = track.values.slice( i, i + 4 );

						_quatA.fromArray( flatQuaternion );

						// Parent rest world rot * track rot * rest world rot inverse
						_quatA
							.premultiply( parentRestWorldRotation )
							.multiply( restRotationInverse );

						_quatA.toArray( flatQuaternion );

						flatQuaternion.forEach( ( v, index ) => {
							track.values[ index + i ] = v;
						} );

					}

					tracks.push(
						new THREE.QuaternionKeyframeTrack(
							`${vrmNodeName}.${propertyName}`,
							track.times,
							track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 2 === 0 ? - v : v ) ),
						),
					);

				} else if ( track instanceof THREE.VectorKeyframeTrack ) {

					const value = track.values.map( ( v, i ) => ( vrm.meta?.metaVersion === '0' && i % 3 !== 1 ? - v : v ) * hipsPositionScale );
					tracks.push( new THREE.VectorKeyframeTrack( `${vrmNodeName}.${propertyName}`, track.times, value ) );

				}

			}

		} );

		return new THREE.AnimationClip( 'vrmAnimation', clip.duration, tracks );

	} );

}
